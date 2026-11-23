"""Read named assignments from the printed columns, including wrapped crew lists."""
import io
import re
from collections import Counter
from dataclasses import dataclass

import pdfplumber


@dataclass(frozen=True)
class Event:
    section: str
    label: str
    start: str
    end: str
    crew: tuple
    brief: str = ""
    formation: str = ""


def names(cell):
    # Each crew column uses LAST, FIRST. Ground lists separate people by commas.
    return tuple((last.strip(), first) for last, first in re.findall(r"([A-Z][A-Z'’ .-]*?),\s*([A-Z][A-Z'’-]*)\b",
                            cell.upper().replace("#", "").strip(" ,")))


def layout(headers):
    if (any(h in headers for h in ("TYPE EVENT", "EVENT", "TYPE SIM EVENT SIM")) and
            any(h in headers for h in ("SCHD T/O", "T/O", "SCHD T/O ICAO")) and
            any(h in headers for h in ("BRIEF TIME", "BRIEF"))):
        if any("SIM" in h or h == "DEVICE" for h in headers):
            return "Simulator"
        if any("LAND" in h for h in headers):
            return "Flight"
    if headers[:5] == ["TIME", "EVENT", "LOCATION", "PERSON", "INSTRUCTOR"]:
        return "Ground"
    if headers[:4] == ["DUTY/WATCH", "ASSIGNED", "SIGN IN", "SIGN OUT"]:
        return "Duty"
    return None


def assignment(kind, cells, headers):
    if kind == "Duty":
        return Event(kind, cells[0], cells[2], cells[3], names(cells[1]))
    if kind == "Ground":
        start, end = cells[0].split("-")
        return Event(kind, cells[1] or kind, start, end, names(cells[3]) + names(cells[4]))
    def field(*options):
        return next((cells[i] for i, h in enumerate(headers) if h in options), "")

    crew = tuple(person for i, h in enumerate(headers)
                 if re.search(r"INSTRUCTOR|STUDENT|MSN CDR|FRONT SEAT|BACK SEAT|SQUIRE|RED KNIGHT", h)
                 for person in names(cells[i]))
    label = field("TYPE EVENT", "EVENT", "TYPE SIM EVENT SIM")
    start = field("SCHD T/O", "T/O", "SCHD T/O ICAO").split()
    end = field("SCHD LAND", "SCHED LAND", "LAND", "SCHD LAND ICAO").split()
    remark = cells[-1].upper()
    partner = re.search(r"\b(?:LD VS|WG VS|FORM(?:ATION)? PARTNER IS)\s+([A-Z][A-Z'-]*)", remark)
    return Event(kind, label or kind, start[0] if start else "", end[0] if end else "",
                 crew, field("BRIEF TIME", "BRIEF"), partner[0] if partner else "")


def read_schedule(pdf):
    events, text, sections = [], [], set()
    active = None
    incomplete = False
    unassigned = []
    with pdfplumber.open(io.BytesIO(pdf)) as document:
        for page in document.pages:
            text.append(page.extract_text() or "")
            words = page.extract_words()
            markers = []
            for table in page.find_tables():
                header = table.rows[0]
                if any(cell is None for cell in header.cells):
                    continue
                labels = [" ".join((v or "").upper().split()) for v in table.extract()[0]]
                kind = layout(labels)
                if kind:
                    sections.add(kind)
                    markers.append((header.bbox[1], header.bbox[3], (kind, labels, header.cells)))
                    if kind == "Duty":
                        markers.append((table.bbox[3], table.bbox[3], None))
            top = 0
            for boundary, resume, following in sorted(markers, key=lambda m: m[0]) + [(page.height, 0, active)]:
                if active:
                    kind, labels, bounds = active
                    lines = {}
                    for word in words:
                        if top <= word["top"] < boundary:
                            row = lines.setdefault(round(word["top"]), [[] for _ in bounds])
                            for i, cell in enumerate(bounds):
                                if cell[0]-1 <= word["x0"] < cell[2]-1:
                                    row[i].append(word["text"])
                                    break
                    current = None
                    for _, row in sorted(lines.items()):
                        cells = [" ".join(col) for col in row]
                        if kind == "Ground":
                            starts = re.fullmatch(r"\d{4}-\d{4}", cells[0])
                        elif kind == "Duty":
                            starts = re.fullmatch(r"\d{4}", cells[2])
                        else:
                            starts = any(re.match(r"^\d{4}\b", value) for h, value in zip(labels, cells)
                                         if h in ("SCHD T/O", "T/O", "SCHD T/O ICAO"))
                        if starts:
                            if current:
                                events.append(assignment(kind, current, labels))
                            current = cells
                        elif current and not cells[0]:
                            current = [" ".join(filter(None, pair)) for pair in zip(current, cells)]
                        elif current:
                            events.append(assignment(kind, current, labels))
                            current = None
                        if not starts and current is None and any("," in c for c in cells):
                            unassigned.append(" ".join(cells))
                    if current:
                        events.append(assignment(kind, current, labels))
                if boundary < page.height:
                    top, active = resume, following
            # Duty is limited to its bordered table. Other sections span pages.
    raw = "\n".join(text).upper()
    totals = re.findall(r"TOTAL (FLY|SIM) EVENTS:\s*(\d+)", raw)
    counts = Counter(e.section for e in events)
    for section, expected in totals:
        if counts["Flight" if section == "FLY" else "Simulator"] != int(expected):
            incomplete = True
    complete = (len(dict(totals)) == 2 and not incomplete and
                ("GROUND EVENTS" not in raw or "Ground" in sections) and
                ("DUTY/WATCH" not in raw or "Duty" in sections))
    return events, raw, complete, " ".join(unassigned)


def personal(schedule, first, last, known):
    events, raw, complete, uncertain = schedule
    if not last or not first or len(first) < 2:
        return None
    identity = (last, first)
    assigned = [event for event in events if identity in event.crew]
    surname = re.compile(r"(?<![A-Z'-])" + re.escape(last) + r"(?![A-Z'-])")
    initials = any((last, first[0]) in event.crew for event in events)
    if initials or surname.search(uncertain) or not complete:
        return None
    if assigned:
        return assigned
    if known and not surname.search(raw):
        return []
    return None
