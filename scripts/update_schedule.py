#!/usr/bin/env python3
"""Download and normalize the official schedule for group ST-11.

The generated JavaScript file is loaded before the application bundle.  A failed
download or an unexpected source layout raises an error before the existing
schedule-data.js is touched, so the published site keeps its last valid data.
"""

from __future__ import annotations

import hashlib
import html as html_module
import json
import os
import re
import tempfile
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path


SOURCE_URL = "https://local.mkgtu.ru/raspisnew/print.php?id_grupp=3027"
OUTPUT_PATH = Path(__file__).resolve().parents[1] / "schedule-data.js"

DAY_NUMBERS = {
    "понедельник": 1,
    "вторник": 2,
    "среда": 3,
    "четверг": 4,
    "пятница": 5,
    "суббота": 6,
    "воскресенье": 0,
}

PAIR_TIMES = {
    1: "08:00–09:30",
    2: "09:40–11:10",
    3: "11:30–13:00",
    4: "13:10–14:40",
    5: "15:00–16:30",
    6: "16:40–18:10",
    7: "18:20–19:50",
    8: "20:00–21:30",
}

KIND_BY_LABEL = {
    "label-success": "lecture",
    "label-danger": "seminar",
    "label-warning": "practice",
    "label-info": "lab",
}

EXCLUDED_TITLE_PARTS = (
    "географ",
    "родная литература",
    "родной язык",
    "русский язык вместо иностранного языка",
)


def normalize(value: str) -> str:
    return " ".join(html_module.unescape(value).replace("\xa0", " ").split())


@dataclass
class Node:
    tag: str
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node | str"] = field(default_factory=list)
    parent: "Node | None" = None

    @property
    def classes(self) -> set[str]:
        return set(self.attrs.get("class", "").split())

    def descendants(self, tag: str | None = None) -> list["Node"]:
        found: list[Node] = []
        for child in self.children:
            if isinstance(child, Node):
                if tag is None or child.tag == tag:
                    found.append(child)
                found.extend(child.descendants(tag))
        return found

    def text(self) -> str:
        parts: list[str] = []
        for child in self.children:
            parts.append(child if isinstance(child, str) else child.text())
        return normalize(" ".join(parts))

    def direct_text(self) -> str:
        return normalize(" ".join(c for c in self.children if isinstance(c, str)))


class TreeParser(HTMLParser):
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = Node("document")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, {key: value or "" for key, value in attrs}, parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in self.VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                return

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(data)


def first_descendant(node: Node, *, tag: str | None = None, class_name: str | None = None) -> Node | None:
    for child in node.descendants(tag):
        if class_name is None or class_name in child.classes:
            return child
    return None


def parse_meta(meta: Node) -> tuple[str, str]:
    room = ""
    kind = "unknown"
    for span in meta.descendants("span"):
        if "label-default" in span.classes:
            room = re.sub(r"^а\.\s*", "", span.text(), flags=re.IGNORECASE)
        for label_class, mapped_kind in KIND_BY_LABEL.items():
            if label_class in span.classes:
                kind = mapped_kind
    return normalize(room), kind


def parse_subject(subject: Node) -> tuple[str, str]:
    title = subject.direct_text()
    teacher_node = first_descendant(subject, tag="em")
    teacher = teacher_node.text() if teacher_node else ""
    return title, teacher


def candidate_from_cells(meta: Node, subject: Node) -> dict[str, str]:
    room, kind = parse_meta(meta)
    title, teacher = parse_subject(subject)
    return {"room": room, "kind": kind, "title": title, "teacher": teacher}


def stable_id(parity: str, weekday: int, pair: int, lesson: dict[str, object]) -> str:
    identity = "|".join(str(lesson.get(key, "")) for key in ("title", "teacher", "room", "kind"))
    suffix = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:8]
    return f"{parity}-{weekday}-{pair}-{suffix}"


def parse_schedule(document: str) -> list[dict[str, object]]:
    parser = TreeParser()
    parser.feed(document)

    if "СТ-11" not in parser.root.text():
        raise RuntimeError("Источник не похож на расписание группы СТ-11")

    lessons: list[dict[str, object]] = []
    parity_nodes: dict[str, Node] = {}
    for node in parser.root.descendants("div"):
        source_id = node.attrs.get("id")
        if source_id in {"nechet", "chet"}:
            parity_nodes[source_id] = node

    if set(parity_nodes) != {"nechet", "chet"}:
        raise RuntimeError("В источнике не найдены обе учебные недели")

    for source_parity, parity in (("nechet", "odd"), ("chet", "even")):
        parity_node = parity_nodes[source_parity]
        panels = [node for node in parity_node.descendants("div") if "panel" in node.classes]

        for panel in panels:
            title_node = first_descendant(panel, class_name="panel-title")
            if title_node is None:
                continue
            day_name = title_node.text().lower()
            if day_name not in DAY_NUMBERS:
                continue
            weekday = DAY_NUMBERS[day_name]

            body = first_descendant(panel, class_name="panel-body")
            if body is None:
                continue

            for row in (node for node in body.descendants("div") if "row" in node.classes):
                cells = [child for child in row.children if isinstance(child, Node) and child.tag == "div"]
                if len(cells) not in {3, 5}:
                    continue
                pair_node = first_descendant(cells[0], tag="b")
                time_node = first_descendant(cells[0], tag="small")
                if pair_node is None or time_node is None:
                    continue
                match = re.search(r"\d+", pair_node.text())
                if match is None:
                    continue
                pair = int(match.group())
                if pair not in PAIR_TIMES:
                    raise RuntimeError(f"Неизвестный номер пары: {pair}")

                if len(cells) == 5:
                    first_group = candidate_from_cells(cells[1], cells[2])
                    chosen = candidate_from_cells(cells[3], cells[4])
                    # The official table sometimes writes the shared lab type only
                    # in the left subgroup column while the right lesson has its own
                    # room and subject.  Preserve that inherited type.
                    if chosen["title"] and chosen["kind"] == "unknown" and first_group["kind"] == "lab":
                        chosen["kind"] = "lab"
                else:
                    chosen = candidate_from_cells(cells[1], cells[2])

                if not chosen["title"]:
                    continue
                lowered_title = chosen["title"].lower()
                if any(part in lowered_title for part in EXCLUDED_TITLE_PARTS):
                    continue

                lesson: dict[str, object] = {
                    "weekday": weekday,
                    "pair": pair,
                    "time": PAIR_TIMES[pair],
                    "title": chosen["title"],
                    "kind": chosen["kind"],
                    "parity": parity,
                    "teacher": chosen["teacher"],
                    "room": chosen["room"],
                }

                # Confirmed correction for ST-11: on odd Fridays the Biology
                # lecture is the first lesson, even if the source still says sixth.
                if parity == "odd" and weekday == 5 and chosen["title"].lower() == "биология" and chosen["kind"] == "lecture":
                    lesson["pair"] = 1
                    lesson["time"] = PAIR_TIMES[1]
                    lesson["customNote"] = "Перенесена с 6-й пары"

                lesson["id"] = stable_id(parity, weekday, int(lesson["pair"]), lesson)
                lessons.append(lesson)

    lessons.sort(key=lambda item: (0 if item["parity"] == "odd" else 1, int(item["weekday"]), int(item["pair"]), str(item["title"])))
    validate(lessons)
    return lessons


def validate(lessons: list[dict[str, object]]) -> None:
    if len(lessons) < 20:
        raise RuntimeError(f"Получено подозрительно мало занятий: {len(lessons)}")
    counts = {parity: sum(item["parity"] == parity for item in lessons) for parity in ("odd", "even")}
    if min(counts.values()) < 8:
        raise RuntimeError(f"Одна из недель почти пустая: {counts}")
    if len({str(item["id"]) for item in lessons}) != len(lessons):
        raise RuntimeError("Обнаружены повторяющиеся идентификаторы занятий")
    for item in lessons:
        if not item["title"] or int(item["pair"]) not in PAIR_TIMES:
            raise RuntimeError(f"Некорректное занятие: {item}")


def download() -> str:
    request = urllib.request.Request(
        SOURCE_URL,
        headers={"User-Agent": "ST-11 schedule updater (+https://zaretsky123.github.io/st-11/)"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        if response.status != 200:
            raise RuntimeError(f"Источник вернул HTTP {response.status}")
        return response.read().decode("utf-8")


def render(lessons: list[dict[str, object]]) -> str:
    payload = json.dumps(lessons, ensure_ascii=False, separators=(",", ":"))
    return (
        "// Generated by scripts/update_schedule.py. Do not edit manually.\n"
        f"globalThis.ST11_SCHEDULE={payload};\n"
        f"globalThis.ST11_SCHEDULE_META={{\"source\":{json.dumps(SOURCE_URL)},\"group\":\"СТ-11\"}};\n"
    )


def write_atomically(content: str) -> None:
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(prefix="schedule-data-", suffix=".js", dir=OUTPUT_PATH.parent)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(content)
        os.replace(temporary_name, OUTPUT_PATH)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main() -> None:
    lessons = parse_schedule(download())
    write_atomically(render(lessons))
    odd = sum(item["parity"] == "odd" for item in lessons)
    even = len(lessons) - odd
    print(f"СТ-11: сохранено {len(lessons)} занятий (нечётная: {odd}, чётная: {even})")


if __name__ == "__main__":
    main()
