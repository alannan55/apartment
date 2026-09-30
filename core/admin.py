from django.contrib import admin

from .models import Adjustment, Allocation, Broker, Charge, Payment, Person, RecurringRule, Room, Stay, Tenancy


@admin.register(Room)
class RoomAdmin(admin.ModelAdmin):
    list_display = ("number", "status", "orientation", "floor", "listing_price", "commission_base", "room_password")
    search_fields = ("number", "notes")
    list_filter = ("status", "floor", "orientation")


@admin.register(Person)
class PersonAdmin(admin.ModelAdmin):
    list_display = ("name", "id_number", "phone", "emergency_name", "emergency_phone")
    search_fields = ("name", "id_number", "phone")


@admin.register(Broker)
class BrokerAdmin(admin.ModelAdmin):
    list_display = ("name", "contact")
    search_fields = ("name", "contact")


class StayInline(admin.TabularInline):
    model = Stay
    extra = 0


@admin.register(Tenancy)
class TenancyAdmin(admin.ModelAdmin):
    list_display = (
        "room",
        "primary_person",
        "start_date",
        "end_date",
        "planned_move_out_date",
        "move_out_date",
        "monthly_rent",
        "status",
        "broker",
    )
    list_filter = ("status", "payment_cycle", "broker")
    search_fields = ("room__number", "primary_person__name", "primary_person__id_number")
    inlines = [StayInline]


@admin.register(Charge)
class ChargeAdmin(admin.ModelAdmin):
    list_display = ("due_date", "direction", "category", "room", "person", "amount", "allocated_amount", "balance", "status")
    list_filter = ("direction", "category", "status", "source")
    search_fields = ("description", "room__number", "person__name")
    date_hierarchy = "due_date"


class AllocationInline(admin.TabularInline):
    model = Allocation
    extra = 0


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = ("date", "direction", "category", "amount", "allocated_amount", "unallocated_amount", "room", "person", "memo")
    list_filter = ("direction", "category", "date")
    search_fields = ("memo", "room__number", "person__name")
    inlines = [AllocationInline]


@admin.register(Adjustment)
class AdjustmentAdmin(admin.ModelAdmin):
    list_display = ("effective_date", "adjustment_type", "amount", "room", "person", "description")
    list_filter = ("adjustment_type",)
    search_fields = ("description", "room__number", "person__name")


@admin.register(RecurringRule)
class RecurringRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "direction", "category", "amount", "frequency", "day_of_month", "active")
    list_filter = ("direction", "category", "frequency", "active")
