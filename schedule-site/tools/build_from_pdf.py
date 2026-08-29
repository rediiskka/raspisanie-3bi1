# -*- coding: utf-8 -*-
"""Собирает data.json из PDF-расписания курса.

PDF даёт шаблон по чётности недели: у каждой пары есть строка «Нечет» и «Четная».
Сайту нужны конкретные даты, поэтому шаблон разворачивается по календарю семестра.

Парсер намеренно простой: он вытаскивает поля и раскладывает их по датам.
Никаких решений о том, как это показывать, он не принимает — это дело сайта.

    python tools/build_from_pdf.py расписание.pdf [--group 3БИ1]
"""
import argparse
import datetime as dt
import json
import os
import re
import sys

import pdfplumber

SITE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(SITE, "data.json")
DEFAULT_GROUP = "3БИ1"

DAYS = ["Понедельник", "Вторник", "Среда", "Четверг",
        "Пятница", "Суббота", "Воскресенье"]
DAY_BY_NAME = {d.lower(): i for i, d in enumerate(DAYS)}

# Колонки таблицы до групп: день, пара, время, чётность.
COL_DAY, COL_PAIR, COL_TIME, COL_PARITY = 0, 1, 2, 3
FIRST_GROUP_COL = 4


# --------------------------------------------------------------------------
# Мелкие помощники
# --------------------------------------------------------------------------

def norm(text):
    """Схлопывает пробелы, сохраняя переводы строк."""
    if not text:
        return ""
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in str(text).split("\n")]
    return "\n".join(ln for ln in lines if ln)


def unwrap_vertical(text):
    """Название дня набрано вертикально, снизу вверх.

    Внутри строки бывают куски по два символа («от» в «Суббота»), поэтому
    переворачивать надо порядок кусков, а не отдельные буквы.
    """
    parts = [x for x in re.split(r"\s+", text or "") if x]
    return "".join(reversed(parts))


def fix_time(text):
    """«8.30-\n10.00» → «8:30–10:00»."""
    t = re.sub(r"\s+", "", text or "").replace(".", ":")
    m = re.match(r"^(\d{1,2}:\d{2})-(\d{1,2}:\d{2})$", t)
    return m.group(1) + "–" + m.group(2) if m else ""


def parse_date(text):
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", text or "")
    return dt.date(int(m.group(3)), int(m.group(2)), int(m.group(1))) if m else None


def fmt_date(d):
    return d.strftime("%d.%m.%Y")


# --------------------------------------------------------------------------
# Разбор шапки: границы семестра, чётность, праздники
# --------------------------------------------------------------------------

def parse_header(text):
    """Из первой страницы достаём период, старты чётной и нечётной, праздники."""
    period = re.search(r"с\s*(\d{2}\.\d{2}\.\d{4})\s*-\s*(\d{2}\.\d{2}\.\d{4})", text)
    odd = re.search(r"[Нн]ечет\w*\s+недел\w*\s*-?\s*с\s*(\d{2}\.\d{2}\.\d{4})", text)
    even = re.search(r"(?<!е)[Чч]етн\w*\s+недел\w*\s*-?\s*с\s*(\d{2}\.\d{2}\.\d{4})", text)
    holidays = re.findall(r"праздник\w*:?\s*((?:\d{2}\.\d{2}\.\d{4}[,;\s]*)+)", text, re.I)
    return {
        "start": parse_date(period.group(1)) if period else None,
        "end": parse_date(period.group(2)) if period else None,
        "odd_from": parse_date(odd.group(1)) if odd else None,
        "even_from": parse_date(even.group(1)) if even else None,
        "holidays": {parse_date(x) for h in holidays
                     for x in re.findall(r"\d{2}\.\d{2}\.\d{4}", h)},
    }


def parse_groups(header_row):
    """Строка шапки с названиями групп. В одной ячейке их может быть несколько."""
    groups = []
    for ci, cell in enumerate(header_row):
        for name in re.findall(r"\d[А-ЯЁA-Z]+\d*", norm(cell)):
            groups.append((ci, name))
    return groups


# --------------------------------------------------------------------------
# Разбор ячейки занятия
# --------------------------------------------------------------------------

TYPES = r"лекция|практика|семинар|лаборат[а-я]*|зач[её]т|экзамен"
DATE = r"\d{2}\.\d{2}\.\d{4}"
TEACHER = r"[А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s*[А-ЯЁ]\."


def join_wrapped(text):
    """Склеивает строки ячейки в одну.

    Дефис в конце строки здесь — часть составного слова («информационно-
    технологическими»), а не слоговой перенос, поэтому он сохраняется,
    а лишний пробел после него не появляется.
    """
    text = re.sub(r"-\n\s*", "-", text)
    return re.sub(r"\n\s*", " ", text).strip()


def is_self_study(text):
    return bool(re.search(r"самостоятельн", text or "", re.I))


def parse_cell(text):
    """Ячейка → список занятий. Прочерк и пустая ячейка означают, что пары нет."""
    text = norm(text)
    if not text or re.fullmatch(r"-{3,}", text.replace("\n", "")):
        return []
    if is_self_study(text):
        return [make_lesson("День самостоятельной работы", text)]
    return [l for l in (parse_one(ch) for ch in split_lessons(text)) if l["subject"]]


def split_lessons(text):
    """Режет ячейку на отдельные занятия.

    Занятие заканчивается строкой с аудиторией или подгруппой; новое начинается
    либо после неё, либо с даты — в одной клетке бывает два датированных занятия.
    """
    lines = text.split("\n")
    starts = [0]
    for i in range(1, len(lines)):
        prev, cur = lines[i - 1], lines[i]
        opened_by_date = re.match(r"^(?:кроме\s+|до\s+)?" + DATE, cur)
        closed = re.search(r"ауд\.|подгруппа", prev, re.I)
        if opened_by_date or (closed and not re.search(r"ауд\.|подгруппа", cur, re.I)):
            if i not in starts:
                starts.append(i)
    starts.append(len(lines))
    return ["\n".join(lines[a:b]).strip()
            for a, b in zip(starts, starts[1:]) if any(lines[a:b])]


def parse_one(chunk):
    """Одно занятие: предмет, тип, преподаватель, аудитория, подгруппа, оговорки по датам."""
    lesson = make_lesson("", chunk)
    flat = join_wrapped(chunk)

    m = re.search(r"\((" + TYPES + r")[^)]*\)", flat, re.I)
    lesson["type"] = m.group(1).lower() if m else ""

    m = re.search(r"ауд\.?\s*([0-9][0-9A-Za-zА-Яа-я/\-]*)", flat, re.I)
    lesson["room"] = m.group(1) if m else ""

    m = re.search(r"(?:подгруппа\s*([12])|([12])\s*подгруппа)", flat, re.I)
    lesson["subgroup"] = (m.group(1) or m.group(2)) if m else ""

    m = re.search(r"(" + TEACHER + r")", flat)
    lesson["teacher"] = m.group(1) if m else ""

    # «кроме 29.12.2026» и «кроме 01.09.2026, 08.09.2026» — в эти даты пары нет
    for m in re.finditer(r"кроме\s+((?:" + DATE + r"[\s,;]*)+)", flat, re.I):
        lesson["except"] += re.findall(DATE, m.group(1))
    body = re.sub(r"кроме\s+(?:" + DATE + r"[\s,;]*)+", "", flat, flags=re.I).strip()

    # «до 20.10.2026» — пара идёт по эту дату включительно
    m = re.match(r"^до\s+(" + DATE + r")\s*", body, re.I)
    if m:
        lesson["until"] = m.group(1)
        body = body[m.end():]

    # «19.09.2026 …» — пара только в эту дату
    m = re.match(r"^(" + DATE + r")\s*", body)
    if m:
        lesson["onlyOn"] = m.group(1)
        body = body[m.end():]

    lesson["subject"] = clean_subject(body)
    return lesson


def clean_subject(body):
    """Убирает из названия тип, преподавателя, аудиторию и подгруппу.

    Уточнение в скобках вроде «(Волейбол № 2)» остаётся: это часть названия,
    а не тип занятия.
    """
    text = re.sub(r"\s*\((?:" + TYPES + r")[^)]*\)\s*", " ", body, flags=re.I)
    text = re.split(r"\s*(?:" + TEACHER + r")", text)[0]
    text = re.sub(r"\s*ауд\.?.*$", "", text, flags=re.I)
    text = re.sub(r"\s*(?:подгруппа\s*\d|\d\s*подгруппа).*$", "", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip(" -—,")


def make_lesson(subject, raw):
    return {"subject": subject, "type": "", "teacher": "", "room": "",
            "subgroup": "", "spectime": "", "dateOverride": "", "note": "",
            "raw": raw, "except": [], "onlyOn": "", "until": ""}


# --------------------------------------------------------------------------
# Чтение таблицы: день × пара × чётность → ячейка группы
# --------------------------------------------------------------------------

def row_by_columns(table, ri, row, bounds):
    """Раскладывает строку по колонкам с учётом объединения по горизонтали.

    Одна пара может быть общей для нескольких групп — тогда в PDF это одна
    широкая ячейка. pdfplumber кладёт её текст в первую колонку диапазона,
    а остальные оставляет пустыми. Разносим текст во все накрытые колонки.
    """
    result = [""] * len(bounds[:-1])
    for ci, text in enumerate(row):
        box = table.rows[ri].cells[ci] if ci < len(table.rows[ri].cells) else None
        if not text or box is None:
            continue
        left, right = box[0], box[2]
        for col in range(len(bounds) - 1):
            # колонка считается накрытой, если её середина попадает в ячейку
            middle = (bounds[col] + bounds[col + 1]) / 2
            if left - 1 <= middle <= right + 1:
                result[col] = text
    return result


def read_template(pdf, group_col):
    """Собирает шаблон недели и набор времён для каждой пары."""
    template = {}          # (день, номер пары, чётность) → текст ячейки
    times = {}             # (день, номер пары) → «8:30–10:00»
    day = None
    pair = None
    prev_pair = None
    time = ""

    for page in pdf.pages:
        for table in page.find_tables():
            bounds = [c.bbox[0] for c in table.columns] + [table.columns[-1].bbox[2]]
            for ri, raw_row in enumerate(table.extract()):
                row = row_by_columns(table, ri, raw_row, bounds)

                raw_day = norm(row[COL_DAY])
                named_day = None
                if raw_day:
                    name = unwrap_vertical(raw_day).lower()
                    if name in DAY_BY_NAME:
                        named_day = DAY_BY_NAME[name]

                raw_pair = norm(row[COL_PAIR])
                if raw_pair.isdigit():
                    pair = int(raw_pair)
                    time = fix_time(row[COL_TIME]) or time
                    # Блок дня может начаться на новой странице, где название дня
                    # ещё не встретилось. Надёжный признак начала — номер пары
                    # перестал расти: внутри дня они идут по возрастанию.
                    if named_day is None and prev_pair is not None and pair <= prev_pair:
                        day = None if day is None else min(day + 1, 6)
                    prev_pair = pair

                if named_day is not None:
                    day = named_day
                    prev_pair = pair

                parity = norm(row[COL_PARITY]).lower()
                if day is None or pair is None or not parity.startswith(("нечет", "четн")):
                    continue

                odd = parity.startswith("нечет")
                cell = row[group_col] if group_col < len(row) else ""
                if norm(cell):
                    template[(day, pair, odd)] = norm(cell)
                times[(day, pair)] = time

    # Если ячейка объединена по вертикали, текст лежит в строке «Нечет», а строка
    # «Четная» пуста — значит пара идёт каждую неделю. Разные тексты в двух
    # строках означают, что пары действительно отличаются по чётности.
    for (day, pair, odd) in list(template):
        if odd and (day, pair, False) not in template:
            template[(day, pair, False)] = template[(day, pair, True)]
    return template, times


# --------------------------------------------------------------------------
# Развёртка шаблона в конкретные недели
# --------------------------------------------------------------------------

def week_starts(head):
    """Понедельники семестра с их чётностью."""
    start, end = head["start"], head["end"]
    first = start - dt.timedelta(days=start.weekday())
    weeks = []
    monday = first
    while monday <= end:
        weeks.append(monday)
        monday += dt.timedelta(days=7)
    return weeks


def week_is_odd(monday, head):
    """Чётность недели определяется по неделе, с которой начинается нечётная."""
    ref = head["odd_from"] or head["start"]
    ref_monday = ref - dt.timedelta(days=ref.weekday())
    return ((monday - ref_monday).days // 7) % 2 == 0


def expand(template, times, head, group):
    weeks = []
    for monday in week_starts(head):
        odd = week_is_odd(monday, head)
        days = []
        for di in range(7):
            date = monday + dt.timedelta(days=di)
            in_term = head["start"] <= date <= head["end"]
            pairs = []
            # День самостоятельной работы указан в таблице один раз, но относится
            # ко всему дню: ставим его в каждую пару, чтобы день читался целиком.
            self_study = any(is_self_study(template.get((di, pi, odd), ""))
                             for pi in range(1, 8))
            for pi in range(1, 8):
                time = times.get((di, pi), "")
                lessons = []
                if in_term and date not in head["holidays"]:
                    if self_study:
                        lessons = [make_lesson("День самостоятельной работы", "")]
                    else:
                        for src in parse_cell(template.get((di, pi, odd), "")):
                            lessons.extend(place(src, date))
                pairs.append({"pair": "%d пара\n%s" % (pi, time), "lessons": lessons})
            days.append({"day": DAYS[di], "date": fmt_date(date), "pairs": pairs})
        dates = [d["date"] for d in days]
        weeks.append({
            "label": "НЕЧЁТНАЯ неделя" if odd else "ЧЁТНАЯ неделя",
            "odd": odd,
            "range": dates[0] + " – " + dates[-1],
            "days": days,
        })
    return weeks


def place(src, date):
    """Ставим занятие в дату, если оговорки это позволяют.

    Сайт получает готовый день: считать «кроме» и «до» в уме студенту не нужно.
    """
    stamp = fmt_date(date)
    if stamp in src["except"]:
        return []
    if src["until"] and date > parse_date(src["until"]):
        return []

    if src["onlyOn"]:
        marked = parse_date(src["onlyOn"])
        same_week = marked - dt.timedelta(days=marked.weekday()) == \
                    date - dt.timedelta(days=date.weekday())
        if marked.weekday() == date.weekday():
            if src["onlyOn"] != stamp:
                return []
        else:
            # В таблице дата занятия не совпадает с днём строки. Разрешать это
            # противоречие парсер не вправе: ставим занятие туда, где оно стоит
            # в таблице, и сохраняем дату — сайт покажет заметку.
            if not same_week:
                return []
            lesson = {k: v for k, v in src.items() if k not in ("except", "onlyOn", "until")}
            lesson["dateOverride"] = src["onlyOn"]
            return [lesson]

    lesson = {k: v for k, v in src.items() if k not in ("except", "onlyOn", "until")}
    return [lesson]


# --------------------------------------------------------------------------

def build(path, group, probe_only=False):
    with pdfplumber.open(path) as pdf:
        head_text = norm(pdf.pages[0].extract_text())
        head = parse_header(head_text)
        if not head["start"] or not head["end"]:
            sys.exit("Не удалось прочитать период семестра из шапки PDF")

        header_row = None
        for table in pdf.pages[0].extract_tables():
            for row in table[:5]:
                if any(re.search(r"\d[А-ЯЁ]+\d*", norm(c) or "") for c in row[FIRST_GROUP_COL:]):
                    header_row = row
                    break
            if header_row:
                break
        if not header_row:
            sys.exit("Не найдена строка с названиями групп")

        groups = parse_groups(header_row)
        if probe_only:
            return None, [n for _, n in groups]
        col = next((ci for ci, name in groups if name == group), None)
        if col is None:
            sys.exit("Группа %s не найдена. Есть: %s"
                     % (group, ", ".join(n for _, n in groups)))

        template, times = read_template(pdf, col)

    notes = ["Нечётная неделя — с %s. Чётная — с %s."
             % (fmt_date(head["odd_from"]), fmt_date(head["even_from"]))
             if head["odd_from"] and head["even_from"] else ""]
    if head["holidays"]:
        notes.append("Государственные праздники: "
                     + ", ".join(fmt_date(d) for d in sorted(head["holidays"])) + ".")

    return {
        "title": "РАСПИСАНИЕ занятий группы %s (%s – %s)"
                 % (group, fmt_date(head["start"]), fmt_date(head["end"])),
        "notes": [n for n in notes if n],
        "weeks": expand(template, times, head, group),
    }, [n for _, n in groups]


def data_name(group):
    """Имя файла с данными группы."""
    return "data-%s.json" % group


def write_json(path, payload):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))


def main():
    ap = argparse.ArgumentParser(description="Сборка данных расписания из PDF курса")
    ap.add_argument("pdf", help="PDF с расписанием курса")
    ap.add_argument("--group", default=DEFAULT_GROUP,
                    help="группа (по умолчанию %s)" % DEFAULT_GROUP)
    ap.add_argument("--all", action="store_true", help="собрать все группы из файла")
    ap.add_argument("--dir", default=SITE, help="куда писать файлы")
    ap.add_argument("--list", action="store_true", help="показать список групп и выйти")
    args = ap.parse_args()

    if not os.path.exists(args.pdf):
        sys.exit("Не найден файл: " + args.pdf)

    _, groups = build(args.pdf, args.group if not args.list else None, probe_only=args.list)
    if args.list:
        print("Группы в файле:", ", ".join(groups))
        return

    targets = groups if args.all else [args.group]
    index = []
    for group in targets:
        data, _ = build(args.pdf, group)
        # одна группа кладётся в data.json — сайт читает именно его
        path = os.path.join(args.dir, data_name(group) if args.all else "data.json")
        write_json(path, data)
        n = sum(len(p["lessons"]) for w in data["weeks"] for d in w["days"] for p in d["pairs"])
        index.append({"group": group, "file": data_name(group), "lessons": n})
        print("  %-8s занятий: %4d  ->  %s" % (group, n, os.path.basename(path)))

    if args.all:
        write_json(os.path.join(args.dir, "groups.json"),
                   {"default": DEFAULT_GROUP if DEFAULT_GROUP in groups else groups[0],
                    "groups": index})
        print("Список групп:", os.path.join(args.dir, "groups.json"))


if __name__ == "__main__":
    main()
