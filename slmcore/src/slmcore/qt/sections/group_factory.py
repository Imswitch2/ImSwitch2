"""Extensible selection of Qt group views from backend group-state types."""

from __future__ import annotations



from ...core.engine.section.components import AberrationsState,CGHState,PatternsState
from ...core.engine.state.groups import GroupStateModel
from .group_views import (
    AberrationsGroupView,
    BaseGroupView,
    CghGroupView,
    PatternsGroupView,
)


class GroupViewFactory:
    """Resolve a ``GroupStateModel`` to its Qt projection class."""

    def __init__(self) -> None:
        self._registrations: list[tuple[type[GroupStateModel], type[BaseGroupView]]] = []

    def register(
        self,
        state_type: type[GroupStateModel],
        view_type: type[BaseGroupView],
        *,
        prepend: bool=True,
    ) -> None:
        self._registrations = [
            item for item in self._registrations if item[0] is not state_type
        ]
        item = (state_type,view_type)
        if prepend:
            self._registrations.insert(0,item)
        else:
            self._registrations.append(item)

    def view_type_for(
        self,state: GroupStateModel,
    ) -> type[BaseGroupView]:
        for state_type,view_type in self._registrations:
            if isinstance(state,state_type):
                return view_type
        return BaseGroupView

    def create(self,*,entry,conversion_context,on_edit,render_policy):
        return self.view_type_for(entry.state)(
            entry=entry,
            conversion_context=conversion_context,
            on_edit=on_edit,
            render_policy=render_policy,
        )

    def clone(self) -> "GroupViewFactory":
        clone = type(self)()
        clone._registrations = list(self._registrations)
        return clone


DEFAULT_GROUP_VIEW_FACTORY = GroupViewFactory()
DEFAULT_GROUP_VIEW_FACTORY.register(PatternsState,PatternsGroupView,prepend=False)
DEFAULT_GROUP_VIEW_FACTORY.register(
    AberrationsState,AberrationsGroupView,prepend=False,
)
DEFAULT_GROUP_VIEW_FACTORY.register(CGHState,CghGroupView,prepend=False)
