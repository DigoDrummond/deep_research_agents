"""Deprecated in sprint 2. The planner emits a typed ResearchPlan directly."""


def split_into_subtasks(*_args, **_kwargs):
    raise RuntimeError(
        "split_into_subtasks was removed in sprint 2; "
        "call generate_research_plan() which returns a ResearchPlan"
    )
