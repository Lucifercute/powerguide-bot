from typing import Literal
from pydantic import BaseModel, Field

class Node(BaseModel):
    id:str
    label:str
    detail:str


class Edge(BaseModel):
    source:str
    target:str
    label:str


class Metric(BaseModel):
    label:str
    value:str
    evidence:str


class SlidePlan(BaseModel):
    number:int
    title:str
    message:str
    visual_type:Literal['cover','process','timeline','matrix','before_after','comparison','bar_chart','stack_map','network','table','closing','text']
    composition:str
    nodes:list[Node]
    edges:list[Edge]
    metrics:list[Metric]
    evidence:list[str]


class VisualPlan(BaseModel):
    title:str
    slides:list[SlidePlan]


class Illustration(BaseModel):
    id:str
    slide:int
    aspect_ratio:Literal['1:1','4:3','3:4','16:9']
    prompt:str
    alt:str


class Deck(BaseModel):
    title:str
    html:str
    images:list[Illustration] = Field(default_factory=list, max_length=3)
