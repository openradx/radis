from datetime import date


def calculate_age(born: date, now: date = date.today()) -> int:
    return now.year - born.year - ((now.month, now.day) < (born.month, born.day))
