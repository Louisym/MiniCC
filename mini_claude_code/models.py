from pydantic import BaseModel, Field, Discriminator, Tag
from typing import Optional, Literal, Annotated, Union

class ContentBlock(BaseModel):
    type: Literal['text', 'tool_use', 'tool_result']

class ToolContentBlock(ContentBlock):
    type: Literal['tool_use'] = 'tool_use'
    id: str
    name: str = ''
    input: str = ''

class TextContentBlock(ContentBlock):
    type: Literal['text'] = 'text'
    text: str = ''

class ToolResultContentBlock(ContentBlock):
    type: Literal['tool_result'] = 'tool_result'
    id: str
    name: str = ''
    output: str = ''
    is_error: Optional[bool] = None

# Discriminated Union: Pydantic 根据 type 字段自动选正确的子类反序列化
# 没有这个，model_validate() 只会创建基类 ContentBlock，丢失 text/name 等字段。
# CC 的 Rust 版用 enum variant 的 tag 做同样的事。
AnyContentBlock = Annotated[
    Union[
        Annotated[TextContentBlock, Tag('text')],
        Annotated[ToolContentBlock, Tag('tool_use')],
        Annotated[ToolResultContentBlock, Tag('tool_result')],
    ],
    Discriminator('type'),
]

class Message(BaseModel):
    role: Literal['user', 'assistant', 'tool']
    content: list[AnyContentBlock] = Field(default_factory=list)

    @classmethod
    def user_text(cls, input:str) -> "Message":
        return cls(role='user', content=[TextContentBlock(type='text',text=input)])
    @classmethod
    def tool_result(cls, id, name, output, is_error):
        return cls(role='tool', content = [ToolResultContentBlock(type='tool_result',id=id, name=name,output=output,is_error=is_error)])
    @classmethod
    def tool_use(cls, id, name, input):
        return cls(role='assistant', content=[ToolContentBlock(type='tool_use',id=id, name=name, input=input)])
    
class Session(BaseModel):
    messages: list[Message] = Field(default_factory=list)

