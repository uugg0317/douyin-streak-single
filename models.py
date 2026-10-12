"""请求体 Pydantic 模型。"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from core.config import DEFAULT_CONFIG


class ConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config: dict

    @model_validator(mode="after")
    def _check_known_keys(self) -> "ConfigBody":
        unknown = [k for k in self.config if k not in DEFAULT_CONFIG]
        if unknown:
            raise ValueError("未知配置项：" + ", ".join(map(str, unknown)))
        return self


class RunBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dry: bool | None = None
    dry_run: bool | None = None

    @model_validator(mode="after")
    def _require_dry_flag(self) -> "RunBody":
        if self.dry is None and self.dry_run is None:
            raise ValueError("必须显式指定 dry（true=干跑 / false=真实发送）")
        return self


class LedgerBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    entries: list[dict]


class SelectionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    selected_names: list[str] = Field(default_factory=list)


class EmailConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    smtp_host: str = ""
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_pass: str = ""
    mail_to: str = ""
    subject: str = ""
    body: str = ""


class DeleteFriendBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_name: str


class BatchDeleteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    display_names: list[str] = Field(default_factory=list)
