from pydantic import BaseModel


class NormalizedMessage(BaseModel):
    phone: str
    body: str
    type: str
    type_raw: str
    push_name: str
    message_id: str
    quoted_id: str = ""
    timestamp: int = 0
    content: dict | str = ""
