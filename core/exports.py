from io import BytesIO
from pathlib import Path
from urllib.parse import quote

from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from PIL import Image, ImageDraw, ImageFont

from .models import Charge, Room, Stay, Tenancy


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


def _font(size, bold=False):
    candidates = [
        Path(r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc"),
        Path(r"C:\Windows\Fonts\simhei.ttf"),
        Path(r"C:\Windows\Fonts\simsun.ttc"),
    ]
    for path in candidates:
        if path.exists():
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


def agent_room_status_image(today=None):
    today = today or timezone.localdate()
    room_rows = []
    for room in Room.objects.all().order_by("number"):
        status = room.refresh_status(today=today, save=False)
        if status not in {Room.Status.VACANT, Room.Status.EXPIRING}:
            continue
        tenancy = room.active_tenancy(today)
        status_text = "空房"
        note = f"密码：{room.room_password}" if room.room_password else ""
        if status == Room.Status.EXPIRING and tenancy:
            if tenancy.planned_move_out_date:
                status_text = f"{tenancy.planned_move_out_date:%Y-%m-%d}预计退租"
                note = f"预计 {tenancy.planned_move_out_date:%Y-%m-%d} 退租后可看"
            else:
                status_text = f"{tenancy.end_date:%Y-%m-%d}到期"
                note = f"预计 {tenancy.end_date:%Y-%m-%d} 后可看"
        elif status == Room.Status.EXPIRING:
            status_text = "即将到期"
        room_rows.append(
            {
                "number": room.number,
                "title": f"{room.number}  {status_text}",
                "price": f"¥{room.listing_price:.0f}/月" if room.listing_price else "价格面议",
                "commission": f"佣金基数：¥{room.commission_base:.0f}",
                "meta": " · ".join(filter(None, [room.orientation, room.floor, f"{room.area:g}㎡" if room.area else ""])),
                "note": note,
            }
        )

    width = 1080
    padding = 52
    card_gap = 18
    title_font = _font(42, bold=True)
    subtitle_font = _font(24)
    header_font = _font(28, bold=True)
    body_font = _font(24)
    small_font = _font(22)
    info_font = _font(26, bold=True)

    probe = Image.new("RGB", (width, 100), "white")
    draw = ImageDraw.Draw(probe)
    card_heights = []
    for item in room_rows:
        note_lines = _wrap_text(draw, item["note"], small_font, width - padding * 2 - 40)
        card_heights.append(174 + len(note_lines) * 28)
    info_lines = [
        "佣金规则：一年租付满佣金，短租按租期时长比例计算。",
        "水费9.5/吨，电费1.4/度；物业/网费免。",
        "冬季取暖380/月（11月15日-3月15日）；停车150/月。",
    ]
    height = 210 + 154 + sum(card_heights) + max(len(room_rows) - 1, 0) * card_gap + 80
    image = Image.new("RGB", (width, max(height, 620)), "#F7F8FA")
    draw = ImageDraw.Draw(image)

    draw.rectangle([0, 0, width, 166], fill="#0F766E")
    draw.text((padding, 42), "悦山公寓可出租房态", font=title_font, fill="white")
    draw.text((padding, 104), f"更新：{today:%Y-%m-%d} · 共 {len(room_rows)} 间", font=subtitle_font, fill="#D7FFFA")

    y = 190
    draw.rounded_rectangle([padding, y, width - padding, y + 124], radius=8, fill="white", outline="#D9E2E0")
    for idx, line in enumerate(info_lines):
        draw.text((padding + 24, y + 20 + idx * 34), line, font=info_font, fill="#334155")
    y += 154

    if not room_rows:
        draw.rounded_rectangle([padding, y, width - padding, y + 150], radius=8, fill="white", outline="#D9E2E0")
        draw.text((padding + 28, y + 54), "暂无空房或即将到期房间", font=header_font, fill="#667085")
    for item, card_height in zip(room_rows, card_heights):
        draw.rounded_rectangle([padding, y, width - padding, y + card_height], radius=8, fill="white", outline="#D9E2E0")
        draw.text((padding + 24, y + 22), item["title"], font=header_font, fill="#18212B")
        price_bbox = draw.textbbox((0, 0), item["price"], font=header_font)
        draw.text((width - padding - 24 - (price_bbox[2] - price_bbox[0]), y + 22), item["price"], font=header_font, fill="#0F766E")
        draw.text((padding + 24, y + 70), item["meta"] or "房间信息待补充", font=body_font, fill="#475467")
        draw.text((padding + 24, y + 108), item["commission"], font=body_font, fill="#18212B")
        note_y = y + 144
        for line in _wrap_text(draw, item["note"], small_font, width - padding * 2 - 48):
            draw.text((padding + 24, note_y), line, font=small_font, fill="#B45309")
            note_y += 28
        y += card_height + card_gap

    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def police_report_workbook(today=None):
    today = today or timezone.localdate()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "入住人员"
    sheet.append(["入住人员登记表"])
    sheet.append([f"更新日期：{today:%Y-%m-%d}"])
    sheet.append(["姓名", "身份证", "房间号", "电话", "紧急联系人", "紧急联系人电话", "合同时间"])
    _style_header(sheet, 3)
    stays = Stay.objects.select_related("person", "room", "tenancy").filter(
        Q(end_date__isnull=True) | Q(end_date__gte=today),
        is_active=True,
        start_date__lte=today,
    )
    for stay in stays.order_by("room__number", "person__name"):
        person = stay.person
        if stay.stay_type == Stay.Type.MANAGER:
            contract_text = "管理员"
        elif stay.stay_type == Stay.Type.PERMANENT and stay.tenancy:
            contract_text = stay.tenancy.contract_text_for_report
        else:
            period = f"{stay.start_date:%Y.%m.%d}-{stay.end_date:%Y.%m.%d}" if stay.end_date else f"{stay.start_date:%Y.%m.%d}起"
            contract_text = f"{stay.report_note or '探望、暂住'} {period}"
        sheet.append(
            [
                person.name,
                person.id_number,
                stay.room.number,
                person.phone,
                person.emergency_name,
                person.emergency_phone,
                contract_text,
            ]
        )
    for row in sheet.iter_rows(min_row=4):
        row[1].number_format = "@"
        row[3].number_format = "@"
        row[5].number_format = "@"
    _autosize(sheet, [14, 24, 10, 16, 16, 18, 28])
    sheet.freeze_panes = "A4"
    return workbook


def export_agent_room_status(today=None):
    return _download_response(agent_room_status_image(today), f"悦山公寓房态_{timezone.localdate():%Y%m%d}.png", "image/png")


def export_import_template():
    return _response(import_template_workbook(), "悦山公寓导入模板.xlsx")


def export_police_report(today=None):
    return _response(police_report_workbook(today), "警务报备表.xlsx")
