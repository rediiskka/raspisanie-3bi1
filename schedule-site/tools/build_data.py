# -*- coding: utf-8 -*-
"""
Генерация data.js из Excel-расписания.

Читает .xlsx, разбирает недели/дни/пары/занятия и пишет открытый data.js
(window.SCHEDULE = {...}), который использует сайт.

Запуск:
    python tools/build_data.py                       # берёт xlsx из папки выше
    python tools/build_data.py "путь/к/файлу.xlsx"

Нужен пакет openpyxl:  pip install -r tools/requirements.txt
"""
import sys, os, re, json
import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
SITE = os.path.dirname(HERE)
DEFAULT_XLSX = os.path.join(os.path.dirname(SITE),
                            "Расписание_3БИ1_1полугодие_2026-2027.xlsx")
OUT = os.path.join(SITE, "data.json")

DAYS = ["Понедельник", "Вторник", "Среда", "Четверг",
        "Пятница", "Суббота", "Воскресенье"]


def norm(s):
    return "" if s is None else str(s).replace("\r\n", "\n").strip()


def parse_lesson(text):
    text = norm(text)
    if not text:
        return []
    lessons = []
    for chunk in [c.strip() for c in re.split(r"\n\s*\n", text) if c.strip()]:
        lines = [l.strip() for l in chunk.split("\n") if l.strip()]
        m = re.search(r"\((лекция|практика|семинар|лаборат[а-я]*|зач[её]т|экзамен)[^)]*\)",
                      chunk, re.I)
        ltype = m.group(1).lower() if m else ""
        # «ауд. не указана» — это отсутствие аудитории, а не аудитория «не»,
        # поэтому номер берём только если он начинается с цифры
        ma = re.search(r"ауд\.?\s*([0-9][0-9A-Za-zА-Яа-я/\-]*)", chunk, re.I)
        room = ma.group(1) if ma else ""
        mg = re.search(r"([12])\s*подгруппа", chunk, re.I)
        subgroup = mg.group(1) if mg else ""
        teacher = ""
        for l in lines:
            mt = re.search(r"([А-ЯЁ][а-яё]+\s+[А-ЯЁ]\.\s*[А-ЯЁ]\.)", l)
            if mt:
                teacher = mt.group(1)
                break
        # время в квадратных скобках может стоять отдельной строкой перед названием —
        # пропускаем такие строки, иначе название предмета теряется
        body = [l for l in lines]
        while body and re.fullmatch(r"\[[^\]]*\]", body[0]):
            body.pop(0)
        subj = re.sub(r"^\[[^\]]*\]\s*", "", body[0]) if body else ""
        subj = re.sub(r"\s*\([^)]*\)\s*$", "", subj).strip()
        # Занятие может быть подписано своей датой — значит в таблице оно стоит
        # не в своей клетке. Парсер только сохраняет дату, раскладывает уже сайт.
        date_override = ""
        md = re.match(r"^(\d{2}\.\d{2}\.\d{4})\s*[—-]\s*(.+)$", subj)
        if md:
            date_override = md.group(1)
            subj = md.group(2).strip()
        mtm = re.search(r"\[([0-9]{1,2}:[0-9]{2}[^\]]*)\]", chunk)
        # пояснение в скобках отдельной строкой — это заметка к занятию
        mn = re.search(r"^\((.+)\)$", lines[-1].strip()) if lines else None
        note = mn.group(1).strip() if mn else ""
        lessons.append({
            "subject": subj, "type": ltype, "teacher": teacher, "room": room,
            "subgroup": subgroup, "spectime": mtm.group(1) if mtm else "",
            # поля для ручных правок: перенос на другую дату и заметка со значком
            "dateOverride": date_override, "note": note, "raw": chunk,
        })
    return lessons


def build(xlsx):
    wb = openpyxl.load_workbook(xlsx, data_only=True)
    ws = wb["3БИ1"] if "3БИ1" in wb.sheetnames else wb.active
    date_rows = [7 + 9 * k for k in range(18)]
    weeks = []
    for d in date_rows:
        wlabel = norm(ws.cell(d, 1).value)
        days = []
        for ci, dayname in enumerate(DAYS):
            c = ci + 2
            date = norm(ws.cell(d, c).value)
            pairs = []
            for pr in range(1, 8):
                r = d + pr
                val = ws.cell(r, c).value
                if val is None and c == 5:
                    val = ws.cell(d + 1, 5).value
                pairs.append({"pair": norm(ws.cell(r, 1).value),
                              "lessons": parse_lesson(val)})
            days.append({"day": dayname, "date": date, "pairs": pairs})
        ds = [x["date"] for x in days if x["date"]]
        weeks.append({"label": wlabel, "odd": "неч" in wlabel.lower(),
                      "range": (ds[0] + " – " + ds[-1]) if ds else "", "days": days})
    return {"title": norm(ws["A1"].value),
            "notes": [norm(ws.cell(r, 1).value) for r in (2, 3, 4)],
            "weeks": weeks}


def main():
    xlsx = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_XLSX
    if not os.path.exists(xlsx):
        sys.exit("Не найден файл Excel: " + xlsx)
    data = build(xlsx)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    n = sum(len(p["lessons"]) for w in data["weeks"] for dd in w["days"] for p in dd["pairs"])
    print("Готово:", OUT)
    print("Недель:", len(data["weeks"]), "| занятий:", n)


if __name__ == "__main__":
    main()
