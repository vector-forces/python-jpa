import inspect
import re
from typing import Any, Callable, ClassVar, Generic, Optional, TypeVar, cast, get_args, get_origin, get_type_hints, List

from pydantic import BaseModel, ConfigDict, Field
from bson import ObjectId, errors

from ppa.config import get_collection, get_framework_logger

T = TypeVar("T", bound='DocumentModel')
F = TypeVar("F", bound=Callable[..., Any])
framework_logger = get_framework_logger("repository")


class DocumentModel(BaseModel):
    """
    Base class for all MongoDB models.
    Inherits default configurations and the standard MongoDB _id mapping.
    """
    __collection_name__: ClassVar[str]

    # Automatically inherited and merged by child models
    model_config = ConfigDict(
        populate_by_name=True,
        arbitrary_types_allowed=True,
        json_encoders={ObjectId: str}
    )

    # Universal MongoDB _id mapping
    id: Optional[ObjectId] = Field(default=None, alias="_id")

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        """
        Overrides default Pydantic dump to ensure MongoDB compatibility.
        Defaults to excluding None values and using aliases (id -> _id).
        """
        kwargs.setdefault("exclude_none", True)
        kwargs.setdefault("by_alias", True)
        return super().model_dump(**kwargs)


# Inherit from list[T] so we can use DocumentList[User], DocumentList[Booking], etc.
class DocumentList(list[T]):
    """
    List of documents that can be used to perform bulk operations.
    Inherits default configurations
    """

    def model_dump(self, **kwargs: Any) -> list[dict[str, Any]]:
        return [doc.model_dump(**kwargs) for doc in self]


def query(definition: dict[str, Any]) -> Callable[[F], F]:
    """Annotation to specify a custom MongoDB query template."""

    def decorator(func: F) -> F:
        func.__query_template__ = definition
        return func

    return decorator


def document(name: str) -> Callable[[type[DocumentModel]], type[DocumentModel]]:
    """Class decorator to bind a Pydantic model to a MongoDB collection."""

    def decorator(cls):
        cls.__collection_name__ = name
        return cls

    return decorator


class RepositoryMeta(type):
    def __new__(mcs, name, bases, namespace):
        cls = super().__new__(mcs, name, bases, namespace)

        if name == "IRepository":
            return cls

        entity_cls = mcs._extract_entity_cls(cls)
        if not entity_cls:
            return cls

        typed_entity_cls = cast(type[DocumentModel], entity_cls)

        if not hasattr(typed_entity_cls, "__collection_name__"):
            raise TypeError(
                f"RepositoryDefinitionError: The model '{entity_cls.__name__}' "
                f"used by '{name}' must be annotated with @document(name='...')"
            )

        cls._entity_cls = typed_entity_cls
        valid_fields = set(typed_entity_cls.model_fields.keys())

        for attr_name, attr_value in list(namespace.items()):
            if inspect.isfunction(attr_value) and not attr_name.startswith("_"):

                query_template = getattr(attr_value, "__query_template__", None)
                is_convention = re.match(r"(find_by|find_all_by|exists_by|count_by)_(.*)", attr_name)

                if not is_convention:
                    framework_logger.info("Convention does not exist, relying on user implementation")
                    continue

                if query_template or is_convention is not None:
                    hints = get_type_hints(attr_value)
                    return_type = hints.get("return")

                    if isinstance(query_template, dict):
                        generated_method = mcs._build_annotated_method(attr_name, attr_value, query_template,
                                                                       return_type, typed_entity_cls)
                    else:
                        generated_method = mcs._build_convention_method(
                            attr_name,
                            is_convention,
                            attr_value,
                            valid_fields,
                            return_type,
                            typed_entity_cls,
                        )

                    setattr(cls, attr_name, generated_method)

        return cls

    @staticmethod
    def _extract_entity_cls(cls) -> type[BaseModel] | None:
        for base in getattr(cls, "__orig_bases__", []):
            if get_origin(base) is IRepository:
                args = get_args(base)
                if args and issubclass(args[0], BaseModel):
                    return args[0]
        return None

    @staticmethod
    def _build_annotated_method(
            name: str,
            func: Callable[..., Any],
            template: dict[str, Any],
            return_type: Any,
            entity_cls: type[DocumentModel],
    ) -> Callable[..., Any]:
        sig = inspect.signature(func)
        param_names = [p for p in sig.parameters if p != "self"]

        def wrapper(self, *args: Any, **kwargs: Any) -> Any:
            bound = sig.bind(self, *args, **kwargs)
            bound.apply_defaults()

            ordered_args: list[Any] = []
            for p_name in param_names:
                if p_name in bound.arguments:
                    ordered_args.append(bound.arguments[p_name])

            def substitute_placeholders(node: Any) -> Any:
                if isinstance(node, str) and node.startswith("?"):
                    try:
                        idx = int(node[1:])
                        return ordered_args[idx]
                    except (ValueError, IndexError):
                        return node
                elif isinstance(node, dict):
                    return {k: substitute_placeholders(v) for k, v in node.items()}
                elif isinstance(node, list):
                    return [substitute_placeholders(item) for item in node]
                return node

            evaluated_query = cast(dict[str, Any], substitute_placeholders(template))

            framework_logger.debug(
                "Invoked custom query %s.%s with args=%s kwargs=%s",
                self.__class__.__name__,
                name,
                args,
                kwargs,
            )

            return execute_dynamic_query(self.collection, evaluated_query, "custom", return_type, entity_cls)

        wrapper.__name__ = name
        wrapper.__qualname__ = f"{entity_cls.__name__}Repository.{name}"
        return wrapper

    @staticmethod
    def _build_convention_method(
            name: str,
            match: re.Match[str],
            func: Callable[..., Any],
            valid_fields: set[str],
            return_type: Any,
            entity_cls: type[DocumentModel],
    ) -> Callable[..., Any]:
        prefix, field_name = match.groups()

        if field_name not in valid_fields:
            raise TypeError(
                f"RepositoryDefinitionError: Field '{field_name}' does not exist on model {entity_cls.__name__}")

        mongo_field = "_id" if field_name == "id" else field_name
        sig = inspect.signature(func)
        param_names = [p for p in sig.parameters if p != "self"]

        def wrapper(self, *args: Any, **kwargs: Any) -> Any:
            bound = sig.bind_partial(self, *args, **kwargs)
            bound.apply_defaults()
            actual_value = bound.arguments.get(param_names[0]) if param_names else None

            # Convert string IDs to ObjectId if querying against _id
            if mongo_field == "_id" and isinstance(actual_value, str):
                try:
                    actual_value = ObjectId(actual_value)
                except errors.InvalidId:
                    pass

            query_dict: dict[str, Any] = {mongo_field: actual_value}

            framework_logger.debug(
                "Invoked convention query %s.%s using %s=%r",
                self.__class__.__name__,
                name,
                mongo_field,
                actual_value,
            )

            return execute_dynamic_query(self.collection, query_dict, prefix, return_type, entity_cls)

        wrapper.__name__ = name
        wrapper.__qualname__ = f"{entity_cls.__name__}Repository.{name}"
        return wrapper


def cast_primary_keys(node: Any, current_field: str | None = None) -> Any:
    """Recursively walks a query structure to convert ID strings to native BSON ObjectIds."""
    if isinstance(node, dict):
        new_dict: dict[str, Any] = {}
        for k, v in node.items():
            next_field = current_field if isinstance(k, str) and k.startswith("$") else k
            new_dict[k] = cast_primary_keys(v, next_field)
        return new_dict
    elif isinstance(node, list):
        return [cast_primary_keys(item, current_field) for item in node]
    elif isinstance(node, str) and current_field == "_id":
        try:
            return ObjectId(node)
        except errors.InvalidId:
            return node
    return node


def execute_dynamic_query(
        collection: Any,
        query_dict: dict[str, Any],
        strategy: str,
        return_type: Any,
        entity_cls: type[DocumentModel],
) -> Any:
    processed_query = cast_primary_keys(query_dict)

    framework_logger.debug(
        "Executing MongoDB query on collection [%s] with strategy [%s]: %s",
        collection.name,
        strategy,
        processed_query,
    )

    origin_type = get_origin(return_type) or return_type

    if strategy == "count_by" or origin_type is int:
        return collection.count_documents(processed_query)

    if strategy == "exists_by" or origin_type is bool:
        return collection.count_documents(processed_query, limit=1) > 0

    # UPDATE: Support both standard lists and our new DocumentList
    if origin_type in (list, DocumentList):
        cursor = collection.find(processed_query)
        results = [entity_cls.model_validate(doc) for doc in cursor]
        # Return the specific list type requested
        return DocumentList(results) if origin_type is DocumentList else results

    doc = collection.find_one(processed_query)
    return entity_cls.model_validate(doc) if doc else None


class IRepository(Generic[T], metaclass=RepositoryMeta):
    def __init__(self):
        """
        Automated Framework Constructor.
        Automatically resolves the target collection name from the
        Pydantic model's @document decorator metadata.
        """
        entity_cls = cast(type[DocumentModel], self._entity_cls)
        collection_name = entity_cls.__collection_name__
        self.collection = get_collection(collection_name)
        framework_logger.debug(
            "Initialized repository [%s] for collection [%s]",
            self.__class__.__name__,
            collection_name,
        )

    def save(self, pyd_model: T) -> str:
        # Base model override handles exclude_none and by_alias automatically
        data = pyd_model.model_dump()
        inserted_data = self.collection.insert_one(data)
        return str(inserted_data.inserted_id)

    def save_all(self, pyd_models: List[T]) -> List[str]:
        data = [pyd_model.model_dump() for pyd_model in pyd_models]
        inserted_data = self.collection.insert_many(data)
        return [str(obj_id) for obj_id in inserted_data.inserted_ids]

    def find_all(self) -> DocumentList[T]:
        cursor = self.collection.find()
        # UPDATE: Wrap the list comprehension in DocumentList
        return DocumentList([self._entity_cls.model_validate(doc) for doc in cursor])

    def find(self, query_dict: dict) -> T:
        data = self.collection.find_one(query_dict)
        if not data:
            raise ValueError("No data found")
        return self._entity_cls.model_validate(data)

    def delete(self, resource_id: str) -> bool:
        query_result = self.collection.delete_one({"_id": ObjectId(resource_id)})
        return query_result.deleted_count > 0

    def update(self, resource_id: str, data: dict) -> bool:
        query_result = self.collection.update_one({"_id": ObjectId(resource_id)}, {"$set": data})
        return query_result.modified_count > 0
