from io import BytesIO
from functools import lru_cache
from decimal import Decimal
from pathlib import Path
from urllib.parse import quote
from math import ceil
from unicodedata import east_asian_width

from django.db.models import Q
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from PIL import Image, ImageDraw, ImageFont

from .models import ApartmentSettings, Charge, Room, Stay, Tenancy


HEADER_FILL = PatternFill("solid", fgColor="1F4E79")
HEADER_FONT = Font(color="FFFFFF", bold=True)


def _response(workbook, filename):
    stream = BytesIO()
    workbook.save(stream)
    response = HttpResponse(
        stream.getvalue(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
    return response


def _download_response(content, filename, content_type):
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = f"attachment; filename*=UTF-8''{quote(filename)}"
    return response


def _style_header(sheet, row):
    for cell in sheet[row]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center")


def _autosize(sheet, widths):
    for idx, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(idx)].width = width


def import_template_workbook():
    workbook = Workbook()
    default = workbook.active
    default.title = "说明"
    default.append(["悦山公寓导入模板"])
    default.append(["1. 先填“房间与合同”：一行一间房，当前有合同时可把主租客和合同填在同一行。"])
    default.append(["2. 再填“入住人员”：一人一行，用房间号关联；同住人、暂住人、管理员都在这里分行填写。"])
    default.append(["3. 最后按需填“期初账务”：只填接管当天的待收待付或已收已付，历史旧账不建议导入。"])
    default.append(["4. 带 * 的字段必填；二维码图片不通过 Excel 导入，请在网页端房间页上传。"])
    default.column_dimensions["A"].width = 92

    room_headers = [
        "房间号*",
        "房态*",
        "挂牌价",
        "佣金基数",
        "朝向",
        "楼层",
        "面积",
        "房间密码",
        "水费",
        "电费",
        "物业费",
        "网费",
        "取暖费/月",
        "停车费/月",
        "房间备注",
        "主租客姓名",
        "主租客身份证号",
        "主租客电话",
        "紧急联系人",
        "紧急联系人电话",
        "紧急联系人地址",
        "合同开始",
        "合同结束",
        "系统计费起始日期",
        "月租金",
        "付款周期",
        "押金",
        "中介/渠道",
        "实际佣金覆盖",
        "报备展示结束日期",
        "合同状态",
        "合同备注",
        "预计退租日期",
        "预计退还押金",
        "预计退租备注",
    ]
    sheet = workbook.create_sheet("房间与合同")
    sheet.append(room_headers)
    sheet.append(
        [
            "A01",
            "空房",
            3500,
            2000,
            "南",
            "二层",
            18,
            "123456#",
            "9.5/吨",
            "1.4/度",
            "免",
            "免",
            380,
            150,
            "",
            "张三",
            "110101199001011234",
            "13800000000",
            "李四",
            "13900000000",
            "北京市",
            "2026-07-01",
            "2027-06-30",
            "2026-07-01",
            3500,
            "月付",
            3500,
            "某某中介",
            "",
            "",
            "在租",
            "",
            "",
            "",
            "",
        ]
    )
    _style_header(sheet, 1)
    _autosize(sheet, [max(12, min(28, len(header) + 4)) for header in room_headers])
    sheet.freeze_panes = "A2"
    for cell in sheet[2]:
        cell.fill = PatternFill("solid", fgColor="F2F4F7")
    dv_status = DataValidation(type="list", formula1='"空房,在住,合同即将到期,自用,维修中"', allow_blank=False)
    dv_cycle = DataValidation(type="list", formula1='"月付,季付,半年付,年付"', allow_blank=True)
    dv_tenancy_status = DataValidation(type="list", formula1='"在租,未入住,已退租"', allow_blank=True)
    dv_floor = DataValidation(type="list", formula1='"1层,2层"', allow_blank=True)
    sheet.add_data_validation(dv_status)
    sheet.add_data_validation(dv_cycle)
    sheet.add_data_validation(dv_tenancy_status)
    sheet.add_data_validation(dv_floor)
    dv_status.add("B2:B500")
    dv_floor.add("F2:F500")
    dv_cycle.add("Z2:Z500")
    dv_tenancy_status.add("AE2:AE500")

    stay_headers = [
        "房间号*",
        "入住类型*",
        "姓名*",
        "身份证号*",
        "手机号",
        "紧急联系人",
        "紧急联系人电话",
        "紧急联系人地址",
        "入住开始",
        "入住结束",
        "当前在住",
        "报备备注",
        "备注",
    ]
    stay_sheet = workbook.create_sheet("入住人员")
    stay_sheet.append(stay_headers)
    stay_sheet.append(["A01", "常住", "李四", "110101199202022345", "13900000000", "张三", "13800000000", "北京市", "2026-07-01", "", "是", "", "同住人"])
    stay_sheet.append(["A01", "管理员", "王管理员", "110101198001010000", "13600000000", "", "", "", "2026-07-01", "", "是", "管理员", "值班"])
    _style_header(stay_sheet, 1)
    _autosize(stay_sheet, [14, 14, 14, 24, 16, 16, 18, 24, 14, 14, 12, 18, 24])
    stay_sheet.freeze_panes = "A2"
    dv_stay = DataValidation(type="list", formula1='"常住,暂住,管理员"', allow_blank=False)
    dv_bool = DataValidation(type="list", formula1='"是,否"', allow_blank=True)
    stay_sheet.add_data_validation(dv_stay)
    stay_sheet.add_data_validation(dv_bool)
    dv_stay.add("B2:B1000")
    dv_bool.add("K2:K1000")

    account_headers = ["方向*", "类别*", "房间号", "日期*", "金额*", "说明", "备注"]
    account_sheet = workbook.create_sheet("期初账务")
    account_sheet.append(account_headers)
    account_sheet.append(["应收", "房租", "A01", "2026-07-01", 0, "期初应收", "示例为 0，可删除或改成真实余额"])
    _style_header(account_sheet, 1)
    _autosize(account_sheet, [12, 14, 12, 14, 12, 24, 36])
    account_sheet.freeze_panes = "A2"
    dv_money_direction = DataValidation(type="list", formula1='"应收,应付,收,付"', allow_blank=False)
    dv_category = DataValidation(type="list", formula1='"房租,押金,取暖费,中介佣金,工资,产权方租金,维修,水电等费用,换房补差,押金退款,其他"', allow_blank=True)
    account_sheet.add_data_validation(dv_money_direction)
    account_sheet.add_data_validation(dv_category)
    dv_money_direction.add("A2:A1000")
    dv_category.add("B2:B1000")
    return workbook


def chinese_font_path(bold=False):
    candidates = [
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path(r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
        Path(r"C:\Windows\Fonts\simsun.ttc"),
    ]
    for path in candidates:
        if path.exists():
            return path
    return None


@lru_cache(maxsize=16)
def _font(size, bold=False):
    path = chinese_font_path(bold)
    if path:
        return ImageFont.truetype(str(path), size)
    return ImageFont.load_default()


def _wrap_text(draw, text, font, max_width):
    text = str(text or "")
    if not text:
        return [""]
    lines = []
    current = ""
    for char in text:
        if char == "\n":
            lines.append(current)
            current = ""
            continue
        trial = current + char
        if draw.textbbox((0, 0), trial, font=font)[2] <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = char
    if current:
        lines.append(current)
    return lines


AGENT_FEE_LABELS = {
    "water_fee": "水费", "electricity_fee": "电费", "property_fee": "物业费",
    "internet_fee": "网费", "heating_fee": "取暖费", "parking_fee": "停车费",
}


def agent_room_status_data(today=None, *, include_commission=True, include_password=True):
    today = today or timezone.localdate()
    all_rooms = list(Room.objects.order_by("number"))
    settings = ApartmentSettings.objects.filter(pk=1).first()
    public_fees = settings.fee_defaults.get("agent_fees", {}) if settings else {}

    def displayed_fees(room):
        fees = []
        for field, label in AGENT_FEE_LABELS.items():
            value = public_fees.get(field, getattr(room, field))
            if field in {"property_fee", "internet_fee"} and str(value).strip() in {"0", "0.0", "0.00"}:
                value = "免费"
            if field in {"heating_fee", "parking_fee"}:
                value = f"{Decimal(str(value)).normalize():f}元/月"
            if field == "parking_fee" and public_fees.get("parking_annual_fee") not in (None, ""):
                value += f" · {Decimal(str(public_fees['parking_annual_fee'])).normalize():f}元/包年"
            fees.append({"label": label, "value": str(value)})
        return fees

    room_rows = []
    for room in all_rooms:
        status = room.refresh_status(today=today, save=False)
        if status not in {Room.Status.VACANT, Room.Status.EXPIRING}:
            continue
        tenancy = room.active_tenancy(today)
        if tenancy and tenancy.end_date < today and not tenancy.planned_move_out_date:
            continue
        if tenancy and Tenancy.objects.filter(previous_tenancy=tenancy, status=Tenancy.Status.UPCOMING).exists():
            continue
        vacant = status == Room.Status.VACANT
        availability = "随时可看 · 可入住"
        note = f"看房密码：{room.room_password}" if include_password and vacant and room.room_password else ""
        if not vacant:
            if tenancy:
                available_date = tenancy.planned_move_out_date or tenancy.end_date
                availability = f"预计 {available_date:%m月%d日} 退租后可看"
            else:
                availability = "可看房时间请联系确认"
            note = "退租时间以实际确认为准"
        room_rows.append({
            "number": room.number,
            "vacant": vacant,
            "status_text": "空房可租" if vacant else "即将到期",
            "availability": availability,
            "price": f"¥{room.listing_price.normalize():f}" if room.listing_price is not None else "价格面议",
            "commission": f"佣金基数 ¥{room.commission_base.normalize():f}" if include_commission else "",
            "meta": " · ".join(filter(None, [room.orientation, room.floor, f"{room.area:g}㎡" if room.area else ""])),
            "note": note,
            "fees": displayed_fees(room),
        })
    # Only the rooms included in this publication determine whether fees differ.
    fee_sets = {tuple(fee["value"] for fee in item["fees"]) for item in room_rows}
    uniform_fees = len(fee_sets) <= 1
    fees = room_rows[0]["fees"] if room_rows else displayed_fees(all_rooms[0] if all_rooms else Room())
    room_rows.sort(key=lambda item: (not item["vacant"], item["number"]))
    return {
        "today": today, "room_rows": room_rows, "fees": fees, "uniform_fees": uniform_fees,
        "vacant_count": sum(item["vacant"] for item in room_rows),
        "expiring_count": sum(not item["vacant"] for item in room_rows),
    }


def agent_room_status_image(today=None, *, include_commission=True, include_password=True):
    data = agent_room_status_data(today, include_commission=include_commission, include_password=include_password)
    width, padding, gap = 1080, 48, 20
    content_width = width - 2 * padding
    title_font, room_font = _font(44, bold=True), _font(38, bold=True)
    price_font, body_font = _font(42, bold=True), _font(25)
    small_font, label_font = _font(23), _font(24, bold=True)
    probe = ImageDraw.Draw(Image.new("RGB", (width, 100)))
    fee_col_width = (content_width - 2 * gap) // 3
    fee_rows = []
    if data["uniform_fees"]:
        for offset in (0, 3):
            row = []
            for fee in data["fees"][offset:offset + 3]:
                row.append((fee, _wrap_text(probe, fee["value"], body_font, fee_col_width - 40)))
            fee_rows.append((row, 65 + max(len(lines) for _, lines in row) * 34))
    fee_height = 62 + sum(height for _, height in fee_rows) + (12 if fee_rows else 64)
    cards = []
    for item in data["room_rows"]:
        lines = [(item["meta"] or "房间信息待补充", body_font, "#64748B")]
        if item["commission"]:
            lines.append((item["commission"], small_font, "#475569"))
        if item["note"]:
            lines.append((item["note"], small_font, "#0F766E" if item["vacant"] else "#B45309"))
        if not data["uniform_fees"]:
            lines.extend((f"{fee['label']}  {fee['value']}", small_font, "#475569") for fee in item["fees"])
        wrapped = [(line, font, color) for text, font, color in lines
                   for line in _wrap_text(probe, text, font, content_width - 72)]
        number_lines = _wrap_text(probe, item["number"], room_font, content_width - 400)
        card_height = 124 + len(number_lines) * 48 + len(wrapped) * 34
        cards.append((item, number_lines, wrapped, card_height))
    commission_lines = _wrap_text(probe, "佣金规则：一年租付满佣金，短租按租期时长比例计算。", small_font, content_width - 48)
    footer_height = 104 + len(commission_lines) * 32
    height = 254 + fee_height + 74 + sum(card[3] + gap for card in cards) + footer_height
    if not cards:
        height += 164
    image = Image.new("RGB", (width, height), "#F2F6F5")
    draw = ImageDraw.Draw(image)
    draw.rectangle([0, 0, width, 224], fill="#123D38")
    draw.text((padding, 34), "悦山公寓 · 可出租房态", font=title_font, fill="white")
    draw.text((padding, 103), f"更新 {data['today']:%Y.%m.%d}   /   共 {len(cards)} 间可租", font=body_font, fill="#C6DDD7")
    draw.text((padding, 160), f"● 空房 {data['vacant_count']} 间", font=label_font, fill="#86E3BE")
    draw.text((padding + 260, 160), f"● 即将到期 {data['expiring_count']} 间", font=label_font, fill="#F9D59C")
    y = 254
    draw.rounded_rectangle([padding, y, width - padding, y + fee_height], radius=18, fill="white")
    draw.text((padding + 24, y + 16), "费用标准", font=label_font, fill="#123D38")
    fy = y + 62
    for row, row_height in fee_rows:
        for index, (fee, lines) in enumerate(row):
            x = padding + index * (fee_col_width + gap)
            draw.rounded_rectangle([x + 12, fy, x + fee_col_width, fy + row_height - 10], radius=12, fill="#F5F8F7")
            draw.text((x + 28, fy + 12), fee["label"], font=small_font, fill="#64748B")
            for line_index, line in enumerate(lines):
                draw.text((x + 28, fy + 46 + line_index * 34), line, font=body_font, fill="#182D29")
        fy += row_height
    if not data["uniform_fees"]:
        draw.text((padding + 24, fy + 8), "各房费用不同，具体标准见对应房间。", font=body_font, fill="#64748B")
    y += fee_height + 24
    draw.text((padding, y), "房间一览", font=label_font, fill="#123D38")
    y += 50
    for item, number_lines, lines, card_height in cards:
        accent = "#0F766E" if item["vacant"] else "#B45309"
        tint = "#E8F5EE" if item["vacant"] else "#FFF2DB"
        draw.rounded_rectangle([padding, y, width - padding, y + card_height], radius=18, fill="white")
        draw.rounded_rectangle([padding + 20, y + 22, padding + 28, y + card_height - 22], radius=4, fill=accent)
        x = padding + 44
        for index, line in enumerate(number_lines):
            draw.text((x, y + 20 + index * 48), line, font=room_font, fill="#182D29")
        price_width = draw.textbbox((0, 0), item["price"], font=price_font)[2]
        draw.text((width - padding - 28 - price_width, y + 18), item["price"], font=price_font, fill=accent)
        if item["price"] != "价格面议":
            draw.text((width - padding - 89, y + 76), " / 月", font=small_font, fill="#64748B")
        sy = y + 28 + len(number_lines) * 48
        draw.rounded_rectangle([x, sy, x + 146, sy + 40], radius=10, fill=tint)
        draw.text((x + 12, sy + 4), item["status_text"], font=small_font, fill=accent)
        draw.text((x + 166, sy + 4), item["availability"], font=small_font, fill=accent)
        ly = sy + 56
        for line, font, color in lines:
            draw.text((x, ly), line, font=font, fill=color)
            ly += 34
        y += card_height + gap
    if not cards:
        draw.rounded_rectangle([padding, y, width - padding, y + 140], radius=18, fill="white")
        draw.text((padding + 30, y + 46), "暂无空房或可出租的即将到期房间", font=body_font, fill="#64748B")
        y += 164
    draw.line((padding, y, width - padding, y), fill="#D7E3DE", width=2)
    for index, line in enumerate(commission_lines):
        draw.text((padding + 24, y + 18 + index * 32), line, font=small_font, fill="#475569")
    draw.text((padding + 24, y + 26 + len(commission_lines) * 32), "取暖季：11月15日至次年3月15日 · 房态以最新确认为准", font=small_font, fill="#64748B")
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def police_report_rows(today=None, *, include_excluded=False):
    today = today or timezone.localdate()
    contracts = {t.room_id: t for t in Tenancy.objects.filter(status=Tenancy.Status.ACTIVE)}
    current = Q(is_active=True) & (Q(start_date__isnull=True) | Q(start_date__lte=today))
    departed = Q(is_active=False, police_departure_token__isnull=False, police_departure_reported_at__isnull=True)
    if not include_excluded:
        departed &= Q(police_departure_required=True)
    rows = []
    for stay in Stay.objects.select_related("person", "room", "tenancy").filter(current | departed).order_by("room__number", "person__name", "id"):
        errors = []
        tenancy = stay.tenancy if stay.tenancy_id else contracts.get(stay.room_id)
        primary = tenancy if tenancy and stay.person_id == tenancy.primary_person_id else None
        if stay.is_active:
            try:
                stay.validate_report(primary)
            except ValidationError as exc:
                errors = exc.messages
        rows.append({"stay": stay, "text": stay.report_text_for_tenancy(tenancy) if stay.is_active else (stay.police_report_text_override or "退租"), "errors": errors,
                     "departed": not stay.is_active, "included": stay.is_active or stay.police_departure_required})
    return rows


def police_report_snapshot(rows):
    errors = [f"{r['stay'].room.number} {r['stay'].person.name}：{'；'.join(r['errors'])}" for r in rows if r["errors"]]
    if errors:
        raise ValidationError(errors)
    snapshot = []
    for row in rows:
        stay = row["stay"]
        person = stay.person
        snapshot.append({
            "stay_id": stay.pk,
            "departure_token": str(stay.police_departure_token) if row["departed"] else None,
            "values": [person.name, person.id_number or "", stay.room.number, person.phone or "",
                       person.emergency_name or "", person.emergency_phone or "", row["text"]],
        })
    return snapshot


def police_report_workbook(today=None, *, snapshot=None):
    today = today or timezone.localdate()
    if snapshot is None:
        snapshot = police_report_snapshot(police_report_rows(today))
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "入住人员"
    sheet.append(["入住人员登记表"])
    sheet.merge_cells("A1:G1")
    sheet["A1"].font = Font(name="宋体", size=24)
    sheet["A1"].alignment = Alignment(horizontal="center", vertical="center")
    sheet.row_dimensions[1].height = 32
    sheet.append(["姓名", "身份证", "房间号", "电话", "紧急联系人", "紧急联系人电话", "合同时间"])
    for row in snapshot:
        sheet.append(row["values"])
    side = Side(style="thin", color="000000")
    border = Border(left=side, right=side, top=side, bottom=side)
    widths = [10, 24.55, 8, 15.22, 18.33, 14.11, 47.55]
    for row in sheet.iter_rows(min_row=2):
        lines = 1
        for cell in row:
            cell.border = border
            cell.font = Font(name="宋体", size=11)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            lines = max(lines, sum(max(1, ceil(sum(2 if east_asian_width(char) in "WF" else 1 for char in line)
                                              / (widths[cell.column - 1] - 2)))
                                   for line in str(cell.value or "").split("\n")))
        sheet.row_dimensions[row[0].row].height = max(32, lines * 16 + 8)
    for row in sheet.iter_rows(min_row=3):
        row[1].number_format = "@"
        row[3].number_format = "@"
        row[5].number_format = "@"
        # User-entered text must remain literal, including a leading '='.
        for cell in row:
            cell.data_type = "s"
    _autosize(sheet, widths)
    sheet.freeze_panes = "A3"
    sheet.print_title_rows = "1:2"
    sheet.print_options.horizontalCentered = True
    sheet.sheet_properties.pageSetUpPr.fitToPage = True
    sheet.page_setup.orientation = "portrait"
    sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
    sheet.page_setup.fitToWidth = 1
    sheet.page_setup.fitToHeight = 0
    sheet.print_area = f"A1:G{sheet.max_row}"
    return workbook


def export_agent_room_status(today=None, *, include_commission=True, include_password=True):
    image = agent_room_status_image(today, include_commission=include_commission, include_password=include_password)
    return _download_response(image, f"悦山公寓房态_{timezone.localdate():%Y%m%d}.png", "image/png")


def export_import_template():
    return _response(import_template_workbook(), "悦山公寓导入模板.xlsx")


def export_police_report(today=None, *, snapshot=None, report_id=None):
    today = today or timezone.localdate()
    filename = f"悦山入住人员报备_{today:%Y%m%d}_{report_id}.xlsx" if report_id else "警务报备表.xlsx"
    return _response(police_report_workbook(today, snapshot=snapshot), filename)
