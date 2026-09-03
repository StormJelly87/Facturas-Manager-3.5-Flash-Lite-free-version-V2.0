from dateutil import parser as dateutil_parser

d1 = dateutil_parser.parse("2026-06-03", dayfirst=True)
print("parse('2026-06-03', dayfirst=True):", d1.strftime('%Y-%m-%d'))

d2 = dateutil_parser.parse("2026-02-10", dayfirst=True)
print("parse('2026-02-10', dayfirst=True):", d2.strftime('%Y-%m-%d'))

d3 = dateutil_parser.parse("03-06-2026", dayfirst=True)
print("parse('03-06-2026', dayfirst=True):", d3.strftime('%Y-%m-%d'))
