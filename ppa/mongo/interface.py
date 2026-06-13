import inspect
import re
from typing import Any, Callable, ClassVar, Generic, TypeVar, cast, get_args, get_origin, get_type_hints, List

from pydantic import BaseModel, ConfigDict
from bson import ObjectId, errors

from ppa.config import get_collection, get_framework_logger
from ppa.mongo import DocumentModel

T = TypeVar("T", bound=DocumentModel)
F = TypeVar("F", bound=Callable[..., Any])
framework_logger = get_framework_logger("repository")

DEFAULT_DOCUMENT_CONFIG: dict[str, Any] = {
    "populate_by_name": True,
    "arbitrary_types_allowed": True,
    "json_encoders": {ObjectId: str}
}


class DocumentModel(BaseModel):
    __collection_name__: ClassVar[str]


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
                    framework_logger.info(f"Convention does not exist for {attr_name}, relying on user implementation")
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
    def _to_object_id(value: Any) -> Any:
        """Convert a string to ObjectId if possible; leave other types untouched."""
        if isinstance(value, ObjectId):
            return value
        if isinstance(value, str):
            try:
                return ObjectId(value)
            except Exception:
                return value
        return value

    @staticmethod
    def _convert_query_ids(query_dict: Any) -> Any:
        """
        Walk a query structure and convert any top-level or nested '_id' keys
        whose values are strings into ObjectId instances.
        """
        if isinstance(query_dict, dict):
            new = {}
            for k, v in query_dict.items():
                if k == "_id":
                    new[k] = RepositoryMeta._to_object_id(v)
                else:
                    new[k] = RepositoryMeta._convert_query_ids(v)
            return new
        if isinstance(query_dict, list):
            return [RepositoryMeta._convert_query_ids(item) for item in query_dict]
        return query_dict

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
            evaluated_query = mcs._convert_query_ids(evaluated_query)

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

        if field_name not in valid_fields and field_name != "id":
            raise TypeError(
                f"RepositoryDefinitionError: Field '{field_name}' does not exist on model {entity_cls.__name__}")

        mongo_field = "_id" if field_name == "id" else field_name
        sig = inspect.signature(func)
        param_names = [p for p in sig.parameters if p != "self"]

        def wrapper(self, *args: Any, **kwargs: Any) -> Any:
            bound = sig.bind_partial(self, *args, **kwargs)
            bound.apply_defaults()
            actual_value = bound.arguments.get(param_names[0]) if param_names else None

            if mongo_field == "_id":
                actual_value = mcs._to_object_id(actual_value)

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


def sanitize_doc(doc: Any) -> Any:
    """Helper to cleanly stringify MongoDB _id types for Pydantic loading."""
    if doc and "_id" in doc:
        doc = dict(doc)
        doc["_id"] = str(doc["_id"])
    return doc


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

    if origin_type is list:
        cursor = collection.find(processed_query)
        return [entity_cls.model_validate(sanitize_doc(doc)) for doc in cursor]

    doc = collection.find_one(processed_query)
    return entity_cls.model_validate(sanitize_doc(doc)) if doc else None


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
        # CRITICAL UPDATE: Add by_alias=True so 'id' becomes '_id' in MongoDB
        data = pyd_model.model_dump()
        inserted_data = self.collection.insert_one(data)
        return str(inserted_data.inserted_id)

    def save_all(self, pyd_models: List[T]) -> List[str]:
        # CRITICAL UPDATE: Add by_alias=True
        data = [pyd_model.model_dump() for pyd_model in pyd_models]
        inserted_data = self.collection.insert_many(data)
        return [str(obj_id) for obj_id in inserted_data.inserted_ids]

    def find_all(self) -> list[T]:
        cursor = self.collection.find()
        # UPDATED: Removed sanitize_doc assumption
        return [self._entity_cls.model_validate(doc) for doc in cursor]

    def find(self, query_dict: dict) -> T:
        data = self.collection.find_one(query_dict)
        if not data:
            raise ValueError("No data found")
        # UPDATED: Removed sanitize_doc assumption
        return self._entity_cls.model_validate(data)

    def delete(self, resource_id: str) -> bool:
        query_result = self.collection.delete_one({"_id": ObjectId(resource_id)})
        return query_result.deleted_count > 0

    def update(self, resource_id: str, data: dict):
        query_result = self.collection.update_one({"_id": ObjectId(resource_id)}, {"$set": data})
        return query_result.modified_count > 0