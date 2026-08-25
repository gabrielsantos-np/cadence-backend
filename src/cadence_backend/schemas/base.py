"""Shared model base.

The wire format is camelCase because the existing Cadence frontend already
consumes these exact payloads. Python stays snake_case internally; the alias
generator bridges the two, so neither side has to hold its nose.
"""

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        extra="forbid",
    )
