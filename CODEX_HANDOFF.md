# 悦山公寓运营系统 Codex 交接

下一位 Codex 开始工作前，请先阅读本文件、`README.md` 和 `使用说明-简明版.md`。

## 用户偏好

- 用户会亲自启动程序。修改后不要运行 `runserver`，除非用户明确要求。
- 可以运行 `python manage.py check`、`python manage.py test` 和迁移检查。
- 以房间为核心组织信息，避免为了自动化制造难以理解的表和状态。
- 自动生成的数据必须能在接近业务语义的位置修改。
- 不迁移旧历史账，系统只保证从接管日期之后的数据正确。
- 页面需要兼顾电脑和手机触摸操作。

## 项目概况

- 技术栈：Django 5.2、SQLite、服务端模板、原生 CSS。
- 项目入口：`manage.py`
- Django 配置：`config/settings.py`
- 核心应用：`core/`
- 模板：`templates/core/`
- 样式：`static/core/app.css`

## 数据库注意事项

项目可能放在 RaiDrive/WebDAV 挂载盘。SQLite 不适合直接在 WebDAV 上运行，可能出现 `database is locked`。

数据库路径支持环境变量：

```powershell
$env:APARTMENT_DB_PATH="$env:LOCALAPPDATA\YueshanApartment\db.sqlite3"
```

`config/settings.py` 已设置 SQLite 30 秒锁等待。自动账单生成也尽量避免无变化 UPDATE，并在账单页面遇到锁时降级为警告。

不要在程序运行时复制 SQLite 数据库。

## 核心模型

- `Room`：房号、房态、价格、佣金基数、密码、固定费用、二维码。
- `Person`：人员身份、电话、紧急联系人。
- `Tenancy`：合同、真实租期、租金、押金、中介、预计退租。
- `Stay`：常住、暂住、管理员入住记录。
- `Charge`：应收或应付账单。
- `Payment`：实际收款或付款流水。
- `Allocation`：流水核销到具体账单的金额。
- `RecurringRule`：周期规则，目前网页端只用于产权方房租。

账务余额始终由：

```text
Charge.amount - Allocation 合计
```

计算，不要额外保存冗余欠款状态。

## 主要业务规则

### 待收

- 固定待收账单只有房租和取暖费。
- 月度账单按“合同/房间 + 账单月份”合并成一行。
- 取暖费周期为每年 `11月15日` 至次年 `3月15日`，按四个周期生成：
  - 11月15日
  - 12月15日
  - 1月15日
  - 2月15日
- 合并行内仍分别显示房租和取暖费。
- 部分收款优先核销房租，再核销取暖费。
- 批量全额收款会为每个房间分别生成一条流水。

### 待付

固定待付账单只展示：

- 中介佣金
- 押金退款
- 产权方房租

维修、罚款、工资、水电和其它临时费用由用户手动记录，只进入流水/全部账务，不进入固定待付列表。

产权方房租使用 `RecurringRule` 配置，可选每月、每季度、每年、金额、付款日和起止日期。

### 账单纠错

- 已结清账单仍然显示。
- 红色表示待处理或部分处理，蓝色表示已结清。
- 账单行显示“修改最近一笔”和“撤销最近一笔”。
- `bill_payment_edit` 只重新核销原账单，不应把金额分配到其它月份。
- 删除 Payment 后，通过关联 Allocation 自动恢复账单余额和状态。

## 关键代码

- 自动账单与核销：`core/services.py`
  - `generate_due_charges`
  - `generate_rent_charges_until`
  - `generate_heating_charges_until`
  - `generate_property_rent_charges_until`
  - `settle_charges`
  - `revise_settlement_payment`
- 账单聚合与页面：`core/views.py`
  - `bill_list`
  - `_receivable_bill_rows`
  - `_payable_bill_rows`
  - `bill_settle`
  - `bill_bulk_settle`
  - `bill_payment_edit`
- 产权方规则页面：
  - `property_rent_rule_list`
  - `property_rent_rule_create`
  - `property_rent_rule_edit`
  - `property_rent_rule_delete`
- 账单模板：`templates/core/bill_list.html`
- 房间总览：`templates/core/room_list.html`
- 签约表单：`templates/core/sign_contract.html`

旧地址 `/collections/` 保留兼容，实际展示新账单页面。正式入口是 `/bills/`。

## 验证命令

不要启动服务。完成修改后运行：

```powershell
python manage.py check
python manage.py test
python manage.py makemigrations --check --dry-run
```

如果确实修改了模型，再创建并检查迁移。不要随意修改或清空正式 `db.sqlite3`。

