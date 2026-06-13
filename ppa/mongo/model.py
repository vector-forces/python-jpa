from typing import Any, ClassVar, Optional
from pydantic import BaseModel, ConfigDict, Field
from bson import ObjectId

DEFAULT_DOCUMENT_CONFIG = ConfigDict(
    populate_by_name=True,
    arbitrary_types_allowed=True,
    json_encoders={ObjectId: str}  # Note: Pydantic v2 warns this is deprecated, but it still works.
)


class DocumentModel(BaseModel):
    """
    Base class for all MongoDB models.
    Inherits default configurations and the standard MongoDB _id mapping.
    """
    __collection_name__: ClassVar[str]

    # Automatically inherited and merged by child models
    model_config = DEFAULT_DOCUMENT_CONFIG

    # Universal MongoDB _id mapping
    id: Optional[ObjectId] = Field(default=None, alias="_id")

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        """
        Overrides default Pydantic dump to ensure MongoDB compatibility.
        Defaults to excluding None values and using aliases (id -> _id).
        """
        # .setdefault() applies our defaults ONLY if the caller didn't explicitly override them
        kwargs.setdefault("exclude_none", True)
        kwargs.setdefault("by_alias", True)

        return super().model_dump(**kwargs)
