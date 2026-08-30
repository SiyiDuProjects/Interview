from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Speaker = Literal["interviewer", "candidate"]
ConnectionRole = Literal["interviewer", "candidate", "client"]


class BrowserLogin(BaseModel):
    access_token: str = Field(min_length=1, max_length=4096)
