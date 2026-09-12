"""Meeting templates: the same evidence-first pipeline, focused for different kinds of meetings.

A template only adds guidance to the analyst's system prompt. It never relaxes the
source-of-truth rules, so a template can change *what is emphasised*, not *what may be invented*.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class MeetingTemplate:
    key: str
    name: str
    description: str
    guidance: str


TEMPLATES: dict[str, MeetingTemplate] = {
    "engineering": MeetingTemplate(
        "engineering", "Engineering meeting",
        "Technical discussion, design, bugs, releases (default).",
        """This is a software engineering meeting.
- Preserve technical terms, service names, API names, repositories, branches, environments,
  ticket identifiers, version numbers, and error messages exactly as spoken.
- Distinguish a proposed technical approach from an agreed decision.
- Tasks are usually implementation, review, testing, deployment, or investigation work.
- Requested changes often concern UI, behaviour, performance, or scope; record who asked for them.""",
    ),
    "standup": MeetingTemplate(
        "standup", "Daily stand-up",
        "Short status round: done, next, blockers.",
        """This is a daily stand-up.
- Organise the summary around progress since last time, what is planned next, and blockers.
- Every blocker that someone states should appear as a task or next step with its owner if named.
- Do not inflate routine status updates into decisions; a decision requires explicit agreement.
- Keep goals and key topics short; the important output is the per-person commitments.""",
    ),
    "one_on_one": MeetingTemplate(
        "one_on_one", "One-on-one",
        "Manager/report or peer check-in: feedback, growth, follow-ups.",
        """This is a one-on-one conversation between two people.
- Focus on feedback given, concerns raised, agreements about priorities, and follow-ups.
- Record personal or career topics only at a high level; never include sensitive details verbatim.
- Attribute commitments carefully: the two speakers are the only possible owners unless a third
  person is explicitly named as responsible.""",
    ),
    "client": MeetingTemplate(
        "client", "Client / stakeholder call",
        "External call: requirements, commitments, deadlines, scope.",
        """This is a call with a client or external stakeholder.
- Requirements, scope changes, and expectations the client states are the most important evidence;
  record them as requested_changes or tasks with the client's exact wording as evidence.
- Capture every promised deliverable, date, price, or quantity exactly; these are commitments.
- Separate what the client asked for from what the team agreed to deliver.
- Note open questions the client expects an answer to as next steps.""",
    ),
    "interview": MeetingTemplate(
        "interview", "Interview",
        "Hiring or research interview: questions, answers, follow-ups.",
        """This is an interview.
- Summarise the topics covered and the candidate's or interviewee's stated experience at a high level.
- Do not evaluate, score, or judge the person; record only what was said.
- Tasks are follow-ups such as sending materials, scheduling next rounds, or checking references.
- Decisions are limited to explicitly agreed next steps.""",
    ),
    "general": MeetingTemplate(
        "general", "General meeting",
        "Any other discussion; balanced extraction.",
        """This is a general meeting with no special structure.
- Apply the standard rules without additional emphasis.""",
    ),
}

DEFAULT_TEMPLATE = "engineering"


def get_template(key: str | None) -> MeetingTemplate:
    return TEMPLATES.get((key or "").strip().lower(), TEMPLATES[DEFAULT_TEMPLATE])


def list_templates() -> list[dict]:
    return [{"key": t.key, "name": t.name, "description": t.description, "default": t.key == DEFAULT_TEMPLATE}
            for t in TEMPLATES.values()]
