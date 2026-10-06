(() => {
  'use strict';
  const form = document.querySelector('[data-accounting-entry]');
  if (form) {
    const bills = JSON.parse(document.getElementById('entry-bills').textContent);
    const contracts = JSON.parse(document.getElementById('entry-contracts').textContent);
    const field = name => form.elements.namedItem(name);
    const label = (name, text) => { field(name).closest('label').querySelector('span').textContent = text; };
    const cents = value => Math.round(Number(value || 0) * 100);
    const money = value => (value / 100).toFixed(2);
    const filterBills = () => {
      const room = field('room').value;
      Array.from(field('charge').options).forEach(option => {
        const bill = bills.find(item => String(item.id) === option.value);
        option.hidden = Boolean(room && bill && String(bill.room || '') !== room);
        option.disabled = option.hidden;
      });
      if (field('charge').selectedOptions[0]?.hidden) field('charge').value = '';
      const tasks = form.querySelector('[data-entry-tasks]');
      const list = form.querySelector('[data-entry-task-list]');
      const matches = room ? bills.filter(item => String(item.room) === room) : [];
      tasks.hidden = !matches.length;
      list.replaceChildren();
      matches.forEach(bill => {
        const button = document.createElement('button');
        button.type = 'button'; button.className = 'button entry-task';
        button.textContent = `${bill.person} · ${bill.description}${bill.period ? ' · ' + bill.period : ''} · ${bill.direction === 'expense' ? '待付' : '待收'} ¥${bill.balance}`;
        button.addEventListener('click', () => {
          field('charge').value = String(bill.id); selectBill(true); update(); field('amount').focus();
        });
        list.append(button);
      });
    };
    const filterContracts = () => {
      const room = field('room').value;
      Array.from(field('tenancy').options).forEach(option => {
        const contract = contracts.find(item => String(item.id) === option.value);
        option.hidden = Boolean(room && contract && String(contract.room) !== room);
        option.disabled = option.hidden;
      });
      if (field('tenancy').selectedOptions[0]?.hidden) field('tenancy').value = '';
      // Auto-select only an unambiguous current contract for tenant fees.
      if (!field('tenancy').value && room && ['rent', 'deposit', 'heating', 'prepaid'].includes(field('category').value)) {
        const today = field('date').value;
        const current = contracts.filter(item => String(item.room) === room && item.status === 'active' && item.start <= today);
        if (current.length === 1) field('tenancy').value = String(current[0].id);
      }
    };
    const selectBill = (overwrite = false) => {
      const bill = bills.find(item => String(item.id) === field('charge').value);
      if (bill) {
        ['direction', 'category', 'room', 'tenancy'].forEach(name => { field(name).value = bill[name] || ''; });
        if (overwrite || !field('amount').value) field('amount').value = bill.balance;
        field('amount').max = bill.balance;
      } else field('amount').removeAttribute('max');
      filterContracts();
    };
    const update = () => {
      const bill = bills.find(item => String(item.id) === field('charge').value);
      const expense = field('direction').value === 'expense';
      const disallowed = expense ? ['rent', 'deposit', 'prepaid', 'deposit_refund'] : ['commission', 'wage', 'property_rent', 'deposit_refund'];
      Array.from(field('category').options).forEach(option => {
        option.hidden = !bill && disallowed.includes(option.value);
        option.disabled = option.hidden;
      });
      if (!bill && field('category').selectedOptions[0]?.hidden) field('category').value = 'other';
      const contract = contracts.find(item => String(item.id) === field('tenancy').value);
      form.querySelector('[data-entry-tenant]').textContent = contract ? `归属租客：${contract.person} · 合同 ${contract.start} 至 ${contract.end}${contract.status === 'ended' ? '（历史合同）' : ''}` : '';
      const refund = form.querySelector('[data-entry-refund]');
      const current = contracts.filter(item => String(item.room) === field('room').value && item.status === 'active' && item.start <= field('date').value);
      const refundContract = contract || (current.length === 1 ? current[0] : null);
      refund.hidden = Boolean(bill || !refundContract?.checkout_url);
      if (refundContract?.checkout_url) {
        refund.href = refundContract.checkout_url;
        refund.textContent = `${refundContract.person} · 办理退租 / 退押金结算`;
      }
      const partial = !bill && field('state').value === 'partial';
      const pending = !bill && field('state').value === 'pending';
      form.querySelectorAll('[data-entry-new]').forEach(node => { node.hidden = Boolean(bill); });
      form.querySelector('[data-entry-partial]').hidden = !partial;
      form.querySelector('[data-entry-due]').hidden = Boolean(bill || pending);
      field('paid_amount').required = partial;
      label('amount', bill ? '本次实际收付金额' : '这件事的总金额');
      label('date', pending ? '应收付日期' : '实际收付日期');
      const amount = cents(field('amount').value);
      const actual = bill ? amount : pending ? 0 : partial ? cents(field('paid_amount').value) : amount;
      const balance = bill ? cents(bill.balance) - actual : amount - actual;
      form.querySelector('[data-entry-summary]').textContent = `${field('direction').value === 'expense' ? '本次实付' : '本次实收'} ¥${money(actual)} · 保存后未结 ¥${money(balance)}`;
      if (!bill && field('category').value === 'prepaid') form.querySelector('[data-entry-summary]').textContent = `本次实收 ¥${money(amount)}，优先抵扣该租客原欠款，剩余部分作为预存。`;
      const matching = !bill && bills.filter(item => item.direction === field('direction').value && item.category === field('category').value && String(item.room || '') === field('room').value && String(item.tenancy || '') === field('tenancy').value);
      form.querySelector('[data-entry-hint]').textContent = bill ? `${bill.description}，尚未结清 ¥${bill.balance}。保存只处理原账单，剩余金额继续提醒。` : matching.length ? `这个对象已有 ${matching.length} 笔同类待办。若是同一事项，请在上方选择原记录。` : '已经登记过待收待付，请选择原事项，避免再记一遍。';
      if (!bill && ['rent', 'deposit', 'heating', 'prepaid', 'deposit_refund'].includes(field('category').value)) form.querySelector('[data-entry-object]').open = true;
      if (!bill && field('category').value === 'rent') form.querySelector('[data-entry-period]').open = true;
    };
    field('charge').addEventListener('change', () => { selectBill(true); update(); });
    field('room').addEventListener('change', () => { filterBills(); selectBill(); filterContracts(); update(); });
    field('category').addEventListener('change', () => { filterContracts(); update(); });
    form.addEventListener('input', update);
    form.addEventListener('change', update);
    form.querySelector('[data-entry-add]').addEventListener('click', () => {
      field('charge').value = ''; field('category').value = 'other'; field('tenancy').value = '';
      field('amount').value = ''; selectBill(); update(); field('category').focus();
    });
    selectBill(); filterBills(); update();
  }
  const checkout = document.querySelector('[data-checkout-settlement]');
  if (checkout) {
    const input = name => checkout.elements.namedItem(name);
    const amount = name => Math.round(Number(input(name).value || 0) * 100);
    const held = Math.round(Number(checkout.dataset.held) * 100);
    const update = event => {
      const left = held - amount('deposit_deduction_amount') - amount('deposit_offset_amount');
      if (event && ['deposit_deduction_amount', 'deposit_offset_amount'].includes(event.target.name)) input('refund_deposit_amount').value = left >= 0 ? (left / 100).toFixed(2) : '';
      checkout.querySelector('[data-checkout-summary]').textContent = left < 0 ? '扣款和抵欠超过实持押金，请核对。' : `扣款、抵欠后可退 ¥${(left / 100).toFixed(2)}。${input('refund_paid').checked ? '保存时同时登记实际退款。' : '保存后进入待付，实际退款时再确认。'}`;
    };
    checkout.addEventListener('input', update); update();
  }
})();
