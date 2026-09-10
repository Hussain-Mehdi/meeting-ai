import html
import io
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from threading import Lock
from urllib.parse import quote

import arabic_reshaper
from bidi.algorithm import get_display
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle


INK = colors.HexColor("#20211F")
MUTED = colors.HexColor("#6F716B")
FAINT = colors.HexColor("#E5E5E1")
PAPER = colors.HexColor("#F7F7F5")
CARD = colors.HexColor("#F1F1EF")
ACCENT = colors.HexColor("#3D7448")
HIGH = colors.HexColor("#9C4944")
FONT_LOCK = Lock()
CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
DASHES = str.maketrans({character: "-" for character in "‐‑‒–—―−"})
RTL_CHARACTERS = re.compile(r"[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufeff]")
OWNER_LABELS = {
    "unknown": "Participant not identified",
    "unknown_participant": "Participant not identified",
    "team": "Team",
    "all_participants": "All participants",
}


def _register_fonts() -> tuple[str, str]:
    """Use a macOS Unicode font and fall back to ReportLab's built-ins."""
    regular_name = "MeetingAIUnicode"
    bold_name = "MeetingAIUnicodeBold"
    with FONT_LOCK:
        registered = set(pdfmetrics.getRegisteredFontNames())
        regular_path = Path("/System/Library/Fonts/Supplemental/Arial Unicode.ttf")
        bold_path = regular_path
        if regular_name not in registered and regular_path.exists():
            pdfmetrics.registerFont(TTFont(regular_name, str(regular_path)))
        if bold_name not in registered and bold_path.exists():
            pdfmetrics.registerFont(TTFont(bold_name, str(bold_path)))
        registered = set(pdfmetrics.getRegisteredFontNames())
        if regular_name not in registered:
            return "Helvetica", "Helvetica-Bold"
        if bold_name not in registered:
            bold_name = regular_name
        pdfmetrics.registerFontFamily(
            regular_name,
            normal=regular_name,
            bold=bold_name,
            italic=regular_name,
            boldItalic=bold_name,
        )
    return regular_name, bold_name


def _plain(value) -> str:
    text = str(value or "").translate(DASHES)
    return CONTROL_CHARACTERS.sub("", text).strip()


def _display_text(value) -> str:
    """Convert logical RTL text to shaped visual order for ReportLab."""
    text = _plain(value)
    if RTL_CHARACTERS.search(text):
        return get_display(arabic_reshaper.reshape(text))
    return text


def _markup(value) -> str:
    return html.escape(_display_text(value)).replace("\n", "<br/>")


def _friendly_owner(value) -> str:
    raw = _plain(value)
    return OWNER_LABELS.get(raw.casefold(), raw or "Participant not identified")


def _iso_datetime(value) -> datetime | None:
    try:
        return datetime.fromisoformat(_plain(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def _long_date(value) -> str:
    parsed = _iso_datetime(value)
    return f"{parsed.strftime('%B')} {parsed.day}, {parsed.year}" if parsed else _plain(value)


def _clock(value) -> str:
    parsed = _iso_datetime(value)
    return parsed.strftime("%I:%M %p").lstrip("0") if parsed else _plain(value)


def report_filename(title: str, started_at: str = "") -> str:
    normalized = unicodedata.normalize("NFC", _plain(title)).replace("_", "-")
    slug = re.sub(r"[^\w-]+", "-", normalized, flags=re.UNICODE).strip("-.").casefold()[:72] or "meeting"
    parsed = _iso_datetime(started_at)
    date_part = parsed.date().isoformat() if parsed else "report"
    return f"{slug}-{date_part}-report.pdf"


def content_disposition(filename: str) -> str:
    unicode_safe = re.sub(r"[\\/:*?\"<>|;\r\n]+", "-", _plain(filename)).strip("-.")
    if not unicode_safe:
        unicode_safe = "meeting-report.pdf"
    ascii_value = unicodedata.normalize("NFKD", unicode_safe).encode("ascii", "ignore").decode()
    ascii_safe = re.sub(r"[^a-zA-Z0-9._-]+", "-", ascii_value).strip("-.")
    if not ascii_safe or ascii_safe.casefold() == "pdf":
        ascii_safe = "meeting-report.pdf"
    return f"attachment; filename=\"{ascii_safe}\"; filename*=UTF-8''{quote(unicode_safe)}"


def _styles(regular_font: str, bold_font: str) -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "brand": ParagraphStyle(
            "ReportBrand", parent=base["Normal"], fontName=bold_font, fontSize=8,
            leading=10, textColor=ACCENT, spaceAfter=4,
        ),
        "title": ParagraphStyle(
            "ReportTitle", parent=base["Title"], fontName=bold_font, fontSize=25,
            leading=30, textColor=INK, alignment=TA_LEFT, spaceAfter=6,
        ),
        "subtitle": ParagraphStyle(
            "ReportSubtitle", parent=base["Normal"], fontName=regular_font, fontSize=9,
            leading=13, textColor=MUTED, spaceAfter=14,
        ),
        "section": ParagraphStyle(
            "ReportSection", parent=base["Heading2"], fontName=bold_font, fontSize=10,
            leading=13, textColor=INK, spaceBefore=16, spaceAfter=7,
        ),
        "subsection": ParagraphStyle(
            "ReportSubsection", parent=base["Heading3"], fontName=bold_font, fontSize=8.5,
            leading=11, textColor=MUTED, spaceBefore=7, spaceAfter=5,
        ),
        "body": ParagraphStyle(
            "ReportBody", parent=base["BodyText"], fontName=regular_font, fontSize=9.3,
            leading=14, textColor=INK, spaceAfter=5, splitLongWords=True,
        ),
        "summary": ParagraphStyle(
            "ReportSummary", parent=base["BodyText"], fontName=regular_font, fontSize=10.4,
            leading=16, textColor=INK, splitLongWords=True,
        ),
        "card_title": ParagraphStyle(
            "ReportCardTitle", parent=base["BodyText"], fontName=bold_font, fontSize=9.1,
            leading=13, textColor=INK, spaceAfter=3, splitLongWords=True,
        ),
        "meta": ParagraphStyle(
            "ReportMeta", parent=base["BodyText"], fontName=regular_font, fontSize=7.6,
            leading=10.5, textColor=MUTED, spaceAfter=3, splitLongWords=True,
        ),
        "evidence": ParagraphStyle(
            "ReportEvidence", parent=base["BodyText"], fontName=regular_font, fontSize=7.8,
            leading=11.5, textColor=MUTED, splitLongWords=True,
        ),
        "empty": ParagraphStyle(
            "ReportEmpty", parent=base["BodyText"], fontName=regular_font, fontSize=8.5,
            leading=12, textColor=MUTED, spaceAfter=4,
        ),
        "bullet": ParagraphStyle(
            "ReportBullet", parent=base["BodyText"], fontName=regular_font, fontSize=9,
            leading=13.5, leftIndent=11, firstLineIndent=-8, bulletIndent=0,
            textColor=INK, spaceAfter=4, splitLongWords=True,
        ),
    }


def _section_heading(label: str, styles: dict[str, ParagraphStyle], width: float):
    heading = Table(
        [[Paragraph(_markup(label.upper()), styles["section"])]],
        colWidths=[width],
        hAlign="LEFT",
    )
    heading.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, -1), 0.45, FAINT),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    return heading


def _bullet_list(values: list, styles: dict[str, ParagraphStyle], empty_message: str):
    cleaned = [_plain(value.get("value") if isinstance(value, dict) else value) for value in values or []]
    cleaned = [value for value in cleaned if value]
    if not cleaned:
        return [Paragraph(_markup(empty_message), styles["empty"])]
    return [Paragraph(f"<bullet>•</bullet>{_markup(value)}", styles["bullet"]) for value in cleaned]


def _list_section(
    label: str,
    values: list,
    styles: dict[str, ParagraphStyle],
    width: float,
    empty_message: str,
):
    return KeepTogether([
        _section_heading(label, styles, width),
        *_bullet_list(values, styles, empty_message),
    ])


def _action_card(
    item: dict,
    styles: dict[str, ParagraphStyle],
    width: float,
    *,
    requested_change: bool = False,
):
    title = item.get("change_text") if requested_change else item.get("task")
    owner_label = "Requested of" if requested_change else "Owner"
    owner = item.get("requested_of") if requested_change else item.get("owner")
    parts = [f"{owner_label}: {_friendly_owner(owner)}"]
    if not requested_change:
        parts.append(f"Status: {_plain(item.get('status') or 'open').capitalize()}")
    if item.get("priority"):
        parts.append(f"Priority: {_plain(item['priority']).capitalize()}")
    if item.get("confidence"):
        parts.append(f"Confidence: {_plain(item['confidence']).capitalize()}")
    deadline = item.get("deadline_original") or item.get("deadline_normalized")
    if deadline:
        parts.append(f"Due: {_plain(deadline)}")

    rows = [
        [Paragraph(_markup(title or "Untitled action"), styles["card_title"])],
        [Paragraph(_markup("  |  ".join(parts)), styles["meta"])],
    ]
    if item.get("evidence"):
        rows.append([Paragraph(f"<b>Evidence:</b> “{_markup(item['evidence'])}”", styles["evidence"])])
    if _plain(item.get("status")).casefold() == "completed":
        border = MUTED
    else:
        border = HIGH if _plain(item.get("priority")).casefold() == "high" else ACCENT
    card = Table(rows, colWidths=[width], hAlign="LEFT", splitByRow=1, splitInRow=1)
    card.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), CARD),
        ("BOX", (0, 0), (-1, -1), 0.4, FAINT),
        ("LINEBEFORE", (0, 0), (0, -1), 2.2, border),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, 0), 8),
        ("BOTTOMPADDING", (0, -1), (-1, -1), 8),
    ]))
    card.spaceAfter = 4
    return card


def _action_group(
    label: str,
    items: list[dict],
    styles: dict[str, ParagraphStyle],
    width: float,
    empty_message: str,
    *,
    requested_change: bool = False,
):
    content = [Paragraph(_markup(label), styles["subsection"])]
    if not items:
        content.append(Paragraph(_markup(empty_message), styles["empty"]))
    else:
        content.extend(
            _action_card(item, styles, width, requested_change=requested_change) for item in items
        )
    return content


def _grouped_section(
    label: str,
    groups: list[list],
    styles: dict[str, ParagraphStyle],
    width: float,
):
    """Keep each group label with its first item and keep the section label with the first group."""
    content = []
    for index, group in enumerate(groups):
        intro = ([_section_heading(label, styles, width)] if index == 0 else []) + group[:2]
        content.append(KeepTogether(intro))
        content.extend(group[2:])
    return content


def _people_group(
    label: str,
    people: list[dict],
    styles: dict[str, ParagraphStyle],
    empty_message: str,
    qualifier_label: str,
):
    content = [Paragraph(_markup(label), styles["subsection"])]
    if not people:
        content.append(Paragraph(_markup(empty_message), styles["empty"]))
        return content
    for person in people:
        name = _markup(person.get("name") or "Name not recorded")
        context = _markup(person.get("context"))
        qualifier = _plain(person.get("importance"))
        qualifier_text = (
            f" <font color='#6F716B'>({_markup(qualifier_label)}: {_markup(qualifier.capitalize())})</font>"
            if qualifier else ""
        )
        text = f"<b>{name}</b>{qualifier_text}"
        if context:
            text += f"<br/><font color='#6F716B'>{context}</font>"
        content.append(Paragraph(text, styles["body"]))
    return content


def _fit_canvas_text(value: str, font_name: str, font_size: float, max_width: float) -> str:
    value = _display_text(value)
    if pdfmetrics.stringWidth(value, font_name, font_size) <= max_width:
        return value
    while value and pdfmetrics.stringWidth(value + "...", font_name, font_size) > max_width:
        value = value[:-1]
    return value.rstrip() + "..."


def build_meeting_report(
    meeting: dict,
    user_name: str,
    aliases: list[str] | tuple[str, ...] = (),
) -> bytes:
    """Create a polished, searchable PDF from the latest persisted meeting report."""
    regular_font, bold_font = _register_fonts()
    styles = _styles(regular_font, bold_font)
    buffer = io.BytesIO()
    title = _plain(meeting.get("title")) or "Untitled meeting"
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=18 * mm,
        leftMargin=18 * mm,
        topMargin=22 * mm,
        bottomMargin=17 * mm,
        title=title,
        author="Meeting AI",
        subject="Private local meeting report",
    )

    alias_set = {
        str(alias).strip().casefold()
        for alias in [user_name, *aliases]
        if str(alias).strip()
    }
    tasks = meeting.get("tasks") or []
    changes = meeting.get("requested_changes") or []
    my_tasks = [task for task in tasks if _plain(task.get("owner")).casefold() in alias_set]
    other_tasks = [task for task in tasks if task not in my_tasks]
    my_open_tasks = [task for task in my_tasks if _plain(task.get("status")).casefold() != "completed"]
    my_completed_tasks = [task for task in my_tasks if _plain(task.get("status")).casefold() == "completed"]
    other_open_tasks = [task for task in other_tasks if _plain(task.get("status")).casefold() != "completed"]
    other_completed_tasks = [task for task in other_tasks if _plain(task.get("status")).casefold() == "completed"]
    my_changes = [change for change in changes if _plain(change.get("requested_of")).casefold() in alias_set]
    other_changes = [change for change in changes if change not in my_changes]

    duration = max(0, int(meeting.get("duration_seconds") or 0))
    minutes = max(1, round(duration / 60)) if duration else 0
    duration_text = f"{minutes} minute{'s' if minutes != 1 else ''}" if minutes else "Duration not recorded"
    date_text = _long_date(meeting.get("started_at"))
    start_text = _clock(meeting.get("started_at"))
    end_text = _clock(meeting.get("ended_at"))
    metadata_parts = [date_text] if date_text else []
    if start_text:
        metadata_parts.append(f"{start_text} to {end_text or 'Not recorded'}")
    metadata_parts.append(duration_text)
    metadata = "  |  ".join(_markup(part) for part in metadata_parts)
    exported = datetime.now().astimezone().strftime("%B %d, %Y at %I:%M %p").replace(" 0", " ")

    summary_box = Table(
        [[Paragraph(_markup(meeting.get("summary") or "No summary was generated."), styles["summary"])]],
        colWidths=[doc.width],
        splitByRow=1,
        splitInRow=1,
    )
    summary_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), PAPER),
        ("BOX", (0, 0), (-1, -1), 0.45, FAINT),
        ("LEFTPADDING", (0, 0), (-1, -1), 12),
        ("RIGHTPADDING", (0, 0), (-1, -1), 12),
        ("TOPPADDING", (0, 0), (-1, -1), 11),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 11),
    ]))

    story = [
        Paragraph("MEETING AI / PRIVATE LOCAL REPORT", styles["brand"]),
        Paragraph(_markup(title), styles["title"]),
        Paragraph(metadata, styles["subtitle"]),
        KeepTogether([
            _section_heading("Summary", styles, doc.width),
            Spacer(1, 6),
            summary_box,
        ]),
        *_grouped_section(
            f"{user_name} - What You Need To Do",
            [
                _action_group(
                    "Open tasks", my_open_tasks, styles, doc.width,
                    f"No open tasks are currently assigned to {user_name}.",
                ),
                _action_group(
                    "Changes requested of you", my_changes, styles, doc.width,
                    f"No additional changes were explicitly requested of {user_name}.",
                    requested_change=True,
                ),
                _action_group(
                    "Completed tasks", my_completed_tasks, styles, doc.width,
                    f"No completed tasks are recorded for {user_name}.",
                ),
            ],
            styles,
            doc.width,
        ),
        _list_section(
            "Decisions", meeting.get("decisions"), styles, doc.width,
            "No confirmed decisions were recorded.",
        ),
        _list_section(
            "Next Steps", meeting.get("next_steps"), styles, doc.width,
            "No next steps were recorded.",
        ),
        _list_section(
            "Goals", meeting.get("goals"), styles, doc.width,
            "No explicit goals were recorded.",
        ),
        _list_section(
            "Key Topics", meeting.get("key_topics"), styles, doc.width,
            "No key topics were recorded.",
        ),
        *_grouped_section(
            "Other Participants - Actions",
            [
                _action_group(
                    "Open tasks", other_open_tasks, styles, doc.width,
                    "No open tasks were confidently assigned to other participants.",
                ),
                _action_group(
                    "Requested changes", other_changes, styles, doc.width,
                    "No other requested changes were recorded.", requested_change=True,
                ),
                _action_group(
                    "Completed tasks", other_completed_tasks, styles, doc.width,
                    "No completed tasks are recorded for other participants.",
                ),
            ],
            styles,
            doc.width,
        ),
    ]

    people = meeting.get("people") or []
    attendees = [person for person in people if person.get("type") == "attendee"]
    mentioned = [person for person in people if person.get("type") == "mentioned"]
    story.extend([
        *_grouped_section(
            "People",
            [
                _people_group(
                    "Confirmed attendees", attendees, styles,
                    "No attendees were confirmed from authoritative meeting metadata.",
                    "Confidence",
                ),
                _people_group(
                    "People mentioned", mentioned, styles,
                    "No additional people were explicitly mentioned.",
                    "Importance",
                ),
            ],
            styles,
            doc.width,
        ),
        Spacer(1, 12),
        Paragraph(
            _markup(
                f"Exported {exported}. This report was generated locally from the saved meeting analysis. "
                "Use the evidence excerpts to verify important assignments and requested changes."
            ),
            styles["empty"],
        ),
    ])

    def decorate_page(canvas, current_doc):
        canvas.saveState()
        if current_doc.page > 1:
            canvas.setFillColor(MUTED)
            canvas.setFont(regular_font, 7)
            header = _fit_canvas_text(title, regular_font, 7, A4[0] - 36 * mm)
            canvas.drawString(18 * mm, A4[1] - 10 * mm, header)
        canvas.setStrokeColor(FAINT)
        canvas.setLineWidth(0.4)
        canvas.line(18 * mm, 11 * mm, A4[0] - 18 * mm, 11 * mm)
        canvas.setFillColor(MUTED)
        canvas.setFont(regular_font, 7)
        canvas.drawString(18 * mm, 7.5 * mm, "Meeting AI - generated locally")
        canvas.drawRightString(A4[0] - 18 * mm, 7.5 * mm, f"Page {current_doc.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=decorate_page, onLaterPages=decorate_page)
    return buffer.getvalue()
