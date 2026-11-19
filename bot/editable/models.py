from typing import Literal
import re
from pydantic import BaseModel, Field, model_validator

# Geometric vocabulary, independent of any private style or prompt text.
FORM_TYPES = {
    **dict.fromkeys(('cover',), 'cover'),
    **dict.fromkeys(('contacts', 'closing'), 'closing'),
    **dict.fromkeys(('kpi', 'cards', 'text_image', 'text_quote', 'text_definition',
                     'text_rule', 'text_steps', 'text_list', 'text_paragraph'), 'text'),
    **dict.fromkeys(('bar', 'hbar', 'line', 'forecast', 'donut', 'stacked100', 'funnel',
                     'waterfall', 'histogram', 'scatter', 'radar', 'range'), 'bar_chart'),
    **dict.fromkeys(('steps', 'cycle'), 'process'),
    'timeline': 'timeline',
    **dict.fromkeys(('tree', 'decision', 'hub', 'network', 'flow'), 'network'),
    **dict.fromkeys(('layers', 'rings'), 'stack_map'),
    'before_after': 'before_after', 'compare': 'comparison', 'matrix': 'matrix',
    **dict.fromkeys(('before_after_text', 'columns', 'table'), 'table'),
}

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
    form:str | None = None
    nodes:list[Node]
    edges:list[Edge]
    metrics:list[Metric]
    evidence:list[str]

    @model_validator(mode='after')
    def normalize_form(self):
        match = re.match(r'\s*ФОРМА:\s*([a-z0-9_]+)\.', self.composition)
        legacy = match[1] if match else None
        if self.form and legacy and self.form != legacy:
            raise ValueError('form не совпадает с ФОРМА в composition')
        self.form = self.form or legacy
        if self.form and FORM_TYPES.get(self.form) != self.visual_type:
            raise ValueError(f'Неизвестная форма или неверный visual_type для {self.form}')
        return self


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
