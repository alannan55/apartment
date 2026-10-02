(() => {
  'use strict';
  const pageKey = location.pathname + location.search;
  const scrollKey = 'apartment-scroll:' + pageKey;
  try {
    const saved = sessionStorage.getItem(scrollKey);
    if (saved !== null) {
      requestAnimationFrame(() => window.scrollTo(0, Number(saved)));
      sessionStorage.removeItem(scrollKey);
    }
  } catch (_) { /* Storage can be disabled by the browser. */ }

  document.querySelectorAll('a[href]').forEach(link => {
    const target = new URL(link.href, location.href);
    if (target.origin !== location.origin) return;
    if (/\/(new|edit|move|checkout|renew)\/$/.test(target.pathname) || target.pathname === '/more/fees/') {
      if (!target.searchParams.has('next')) target.searchParams.set('next', pageKey);
      link.href = target.pathname + target.search;
      link.addEventListener('click', () => {
        try { sessionStorage.setItem(scrollKey, String(window.scrollY)); } catch (_) {}
      });
    }
  });
  document.querySelectorAll('form').forEach(form => {
    form.addEventListener('submit', event => {
      if (form.classList.contains('bill-filters')) form.elements.scope.value = form.elements.month.value ? 'month' : 'all';
      if (form.dataset.confirm && !window.confirm(form.dataset.confirm)) {
        event.preventDefault();
        return;
      }
      if (form.method.toLowerCase() !== 'post') return;
      if (form.dataset.submitting) { event.preventDefault(); return; }
      form.dataset.submitting = 'true';
      // Keep submitter name/value in the request (e.g. cancel planned checkout).
      setTimeout(() => form.querySelectorAll('[type="submit"]').forEach(button => { button.disabled = true; }), 0);
    });
  });
  window.addEventListener('pageshow', () => document.querySelectorAll('form').forEach(form => {
    delete form.dataset.submitting;
    form.querySelectorAll('[type="submit"]').forEach(button => { button.disabled = false; });
  }));

  document.querySelectorAll('table').forEach(table => {
    const headings = Array.from(table.querySelectorAll('thead th')).map(th => th.textContent.trim());
    table.querySelectorAll('tbody tr').forEach(row => Array.from(row.cells).forEach((cell, index) => {
      if (cell.colSpan === 1) cell.dataset.label = headings[index] || '';
    }));
  });

  const rentForm = document.querySelector('[data-monthly-rent]');
  if (rentForm) {
    const rows = Array.from(rentForm.querySelectorAll('[data-rent-row]'));
    const formatMoney = cents => (cents / 100).toFixed(2);
    const update = () => {
      let count = 0, total = 0;
      rows.forEach(row => {
        const draft = row.querySelector('[name^="rent_amount_"]');
        if (draft) {
          const cents = Math.round(Number(draft.value) * 100);
          row.dataset.balance = formatMoney(Number.isFinite(cents) ? cents : 0);
          row.querySelector('[data-rent-balance]').textContent = '¥' + row.dataset.balance;
        }
        if (row.querySelector('[name="rows"]:checked')) {
          count++;
          total += Math.round(Number(row.dataset.balance) * 100);
        }
      });
      rentForm.querySelector('[data-rent-selection-summary]').textContent = `已选 ${count} 户 · 所选未结 ¥${formatMoney(total)}`;
      return {count, total};
    };
    rentForm.querySelectorAll('[data-rent-select]').forEach(button => {
      button.addEventListener('click', () => {
        rows.forEach(row => {
          const checkbox = row.querySelector('[name="rows"]');
          if (!checkbox.disabled) checkbox.checked = button.dataset.rentSelect === 'all';
        });
        update();
      });
    });
    rentForm.addEventListener('input', update);
    rentForm.addEventListener('change', update);
    rentForm.addEventListener('submit', event => {
      const button = event.submitter;
      if (button && button.name === 'single_row') {
        const row = button.closest('[data-rent-row]');
        const input = row.querySelector('[name^="partial_"]');
        const amount = input.value || row.dataset.balance;
        rentForm.dataset.confirm = `确认 ${row.dataset.room} 的 ${rentForm.elements.month.value} 房租已实际收到 ¥${amount}？`;
      } else {
        const {count, total} = update();
        if (!count) {
          event.preventDefault(); event.stopImmediatePropagation();
          window.alert('请先勾选已实际交租的房间。');
          return;
        }
        const partial = rows.find(row => row.querySelector('[name="rows"]:checked') && row.querySelector('[name^="partial_"]')?.value);
        if (partial) {
          event.preventDefault(); event.stopImmediatePropagation();
          window.alert(`${partial.dataset.room} 填写了本次实收金额，请先用该行“记录收款”保存，或清空金额后再批量收齐。`);
          return;
        }
        rentForm.dataset.confirm = `确认所选 ${count} 户的 ${rentForm.elements.month.value} 房租已实际交齐？所选未结共 ¥${formatMoney(total)}，已有预收款会先抵扣，再记录余款。`;
      }
    }, true);
    update();
  }

  const depositForm = document.querySelector('[data-deposit-collection]');
  if (depositForm) {
    const rows = Array.from(depositForm.querySelectorAll('[data-deposit-row]'));
    const update = () => {
      let count = 0, total = 0;
      rows.forEach(row => {
        const target = row.querySelector('[name^="deposit_amount_"]');
        if (target) {
          const balance = Math.max(0, Math.round((Number(target.value) - Number(row.dataset.paid)) * 100));
          row.dataset.balance = (balance / 100).toFixed(2);
          row.querySelector('[data-deposit-balance]').textContent = '¥' + row.dataset.balance;
        }
        if (row.querySelector('[name="rows"]:checked')) {
          count++;
          total += Math.round(Number(row.dataset.balance) * 100);
        }
      });
      depositForm.querySelector('[data-deposit-summary]').textContent = `已选 ${count} 户 · 本次补记 ¥${(total / 100).toFixed(2)}`;
      return {count, total};
    };
    depositForm.querySelectorAll('[data-deposit-select]').forEach(button => {
      button.addEventListener('click', () => {
        rows.forEach(row => {
          const checkbox = row.querySelector('[name="rows"]');
          if (!checkbox.disabled) checkbox.checked = button.dataset.depositSelect === 'all';
        });
        update();
      });
    });
    depositForm.addEventListener('input', update);
    depositForm.addEventListener('change', update);
    depositForm.addEventListener('submit', event => {
      if (event.submitter?.name === 'single_row') {
        const row = event.submitter.closest('[data-deposit-row]');
        const amount = row.querySelector('[name^="partial_"]').value || row.dataset.balance;
        depositForm.dataset.confirm = `确认 ${row.dataset.room} 的押金已实际收到 ¥${amount}？`;
      } else {
        const {count, total} = update();
        const partial = rows.find(row => row.querySelector('[name="rows"]:checked') && row.querySelector('[name^="partial_"]')?.value);
        if (!count || partial) {
          event.preventDefault(); event.stopImmediatePropagation();
          window.alert(partial ? `${partial.dataset.room} 填写了本次实收，请用“记录本笔押金”保存，或清空金额再批量登记。` : '请先勾选已实际收到押金的房间。');
          return;
        }
        depositForm.dataset.confirm = `确认所选 ${count} 户的押金已实际收齐？本次补记 ¥${(total / 100).toFixed(2)}，已登记部分会扣除。`;
      }
    }, true);
    update();
  }

  const stayType = document.getElementById('id_stay_type');
  if (stayType) {
    const update = () => {
      const manager = stayType.value === 'manager';
      const permanent = stayType.value === 'permanent';
      ['start_date', 'end_date', 'emergency_name', 'emergency_phone', 'emergency_address'].forEach(name => {
        const label = document.querySelector(`[data-field="${name}"]`);
        if (!label) return;
        const hidden = name.startsWith('emergency_') ? !permanent : manager;
        label.hidden = hidden;
        // Hidden fields are optional; preserve prefilled personal data when editing.
      });
    };
    stayType.addEventListener('change', update);
    update();
  }

  const roommateContainer = document.getElementById('roommate-forms');
  if (roommateContainer) {
    document.getElementById('add-roommate').addEventListener('click', event => {
      const total = document.getElementById('id_roommates-TOTAL_FORMS');
      const count = Number(total.value);
      if (count >= 10) return;
      const template = document.getElementById('roommate-template');
      const fragment = template.content.cloneNode(true);
      fragment.querySelectorAll('[name], [id], [for]').forEach(element => {
        ['name', 'id', 'for'].forEach(attribute => {
          if (element.hasAttribute(attribute)) element.setAttribute(attribute, element.getAttribute(attribute).replaceAll('__prefix__', String(count)));
        });
      });
      roommateContainer.append(fragment); total.value = count + 1;
      if (count + 1 >= 10) event.currentTarget.disabled = true;
    });
    roommateContainer.addEventListener('click', event => {
      if (!event.target.matches('[data-use-primary-contact]')) return;
      const row = event.target.closest('.roommate-form');
      row.querySelector('[name$="-emergency_name"]').value = document.getElementById('id_person_name').value;
      row.querySelector('[name$="-emergency_phone"]').value = document.getElementById('id_phone').value;
    });
  }

  // Filter a native select without replacing keyboard or touch accessibility.
  document.querySelectorAll('select[name="room"], select[name="new_room"]').forEach(select => {
    if (select.options.length < 10) return;
    const input = document.createElement('input');
    input.type = 'search'; input.className = 'input room-search';
    input.placeholder = '输入房号快速筛选'; input.setAttribute('aria-label', '筛选房间选项');
    select.before(input);
    const options = Array.from(select.options).map(option => option.cloneNode(true));
    input.addEventListener('input', () => {
      const selected = select.value;
      const q = input.value.trim().toLowerCase();
      select.replaceChildren(...options.filter(option => !option.value || option.value === selected || option.textContent.toLowerCase().includes(q)).map(option => option.cloneNode(true)));
      select.value = selected;
    });
  });

  const debounce = (fn, ms = 250) => {
    let timer;
    return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
  };
  const showLines = (element, lines) => {
    element.replaceChildren(...lines.map(line => {
      const p = document.createElement('p'); p.textContent = line; return p;
    }));
  };
  document.querySelectorAll('[data-person-search]').forEach(box => {
    const input = box.querySelector('input');
    const results = box.querySelector('.search-results');
    let sequence = 0;
    const search = debounce(async () => {
      const current = ++sequence;
      if (input.value.trim().length < 2) { results.replaceChildren(); return; }
      try {
        const response = await fetch('/people/lookup/?q=' + encodeURIComponent(input.value.trim()));
        if (!response.ok) throw new Error();
        const data = await response.json();
        if (current !== sequence) return;
        results.replaceChildren();
        data.people.forEach(person => {
          const button = document.createElement('button');
          button.type = 'button'; button.className = 'button';
          button.textContent = `${person.name} · ${person.phone || '未填电话'} · 证件尾号 ${person.id_number.slice(-4)}`;
          button.addEventListener('click', () => {
            Object.entries(person).forEach(([name, value]) => {
              if (name === 'id') return;
              const field = document.getElementById('id_' + name) || (name === 'name' && document.getElementById('id_person_name'));
              if (field) { field.value = value || ''; field.dispatchEvent(new Event('input', {bubbles: true})); }
            });
            results.textContent = '已带入资料，请核对后保存。';
          });
          results.append(button);
        });
        if (!data.people.length) results.textContent = '没有找到，可在下方登记新人员。';
      } catch (_) { results.textContent = '暂时无法查找，可直接填写资料。'; }
    });
    input.addEventListener('input', () => { sequence++; search(); });
  });

  function wirePreview(selector, endpoint, format) {
    const box = document.querySelector(selector);
    if (!box) return;
    const form = box.closest('form');
    let sequence = 0;
    const update = debounce(async () => {
      const current = ++sequence;
      const params = new URLSearchParams(new FormData(form));
      params.delete('csrfmiddlewaretoken'); params.delete('next');
      // Send only calculation inputs; personal details never belong in preview URLs.
      const allowed = endpoint.includes('payments') ? ['room', 'tenancy', 'amount', 'date', 'category', 'direction'] : ['room', 'start_date', 'end_date', 'monthly_rent', 'deposit_amount', 'payment_cycle', 'first_month_discount'];
      for (const key of Array.from(params.keys())) if (!allowed.includes(key)) params.delete(key);
      const required = endpoint.includes('payments') ? ['amount', 'date'] : ['start_date', 'end_date', 'monthly_rent', 'deposit_amount'];
      if (required.some(name => !params.get(name))) {
        showLines(box, [endpoint.includes('payments') ? '填写金额后展示本次抵扣明细。' : '填写完整租期与金额后展示首期费用。']);
        return;
      }
      try {
        const response = await fetch(endpoint + '?' + params.toString());
        const data = await response.json();
        if (current !== sequence) return;
        showLines(box, response.ok ? format(data) : [data.error || '请检查填写内容。']);
      } catch (_) { if (current === sequence) showLines(box, ['预览暂不可用，保存后可在记账页核对。']); }
    });
    form.addEventListener('input', () => { sequence++; update(); });
    form.addEventListener('change', () => { sequence++; update(); });
    update();
  }
  wirePreview('[data-payment-preview]', '/payments/preview/', data => [
    `本次对象：${data.person}`, ...data.items,
    `${data.remaining_label}：¥${data.remaining}`,
    '这里只是预览，点击确认保存后才会记账。'
  ]);
  wirePreview('[data-contract-preview]', '/tenancies/preview/', data => [
    `首期房租 ¥${data.rent} · 押金 ¥${data.deposit} · 小计 ¥${data.subtotal}`, data.note
  ]);
  const moveBox = document.querySelector('[data-move-preview]');
  if (moveBox) {
    const form = moveBox.closest('form');
    const update = () => {
      const date = new Date((form.elements.move_date.value || '') + 'T12:00:00');
      const rent = Number(form.elements.new_monthly_rent.value);
      if (!Number.isFinite(date.getTime()) || !form.elements.new_monthly_rent.value) return;
      const days = new Date(date.getFullYear(), date.getMonth() + 1, 0).getDate();
      const remaining = days - date.getDate() + 1;
      const diff = (rent - Number(moveBox.dataset.oldRent)) * remaining / days;
      showLines(moveBox, [`按本月剩余 ${remaining} / ${days} 天计算，${diff >= 0 ? '应补收' : '应退'} ¥${Math.abs(diff).toFixed(2)}。`, '需要优惠或协商调整时，填写调整后差价；押金继续保留。']);
    };
    form.addEventListener('input', update); update();
  }
  const agentShare = document.querySelector('[data-agent-share]');
  if (agentShare) {
    const status = document.querySelector('[data-agent-share-status]');
    const copyImage = document.querySelector('[data-agent-copy]');
    let imageFile;
    let imageError = '';
    let fileShareSupported = false;
    const shareSupported = Boolean(navigator.share && navigator.canShare);
    const copySupported = Boolean(navigator.clipboard?.write && window.ClipboardItem);
    copyImage.hidden = !copySupported;
    const fallback = () => {
      if (!window.isSecureContext) {
        status.textContent = '当前使用 HTTP 地址，浏览器限制直接分享图片。请保存图片后在微信发送，或在手机长按下方图片保存。';
      } else if (imageError) {
        status.textContent = imageError;
      } else {
        status.textContent = '当前浏览器不支持直接分享图片。可复制图片后在微信聊天框粘贴，或保存图片后发送。';
      }
    };
    const prepareImage = async () => {
      agentShare.disabled = true;
      copyImage.disabled = true;
      try {
        // Prepare before the click so sharing and copying retain user activation.
        const response = await fetch(agentShare.dataset.imageUrl, {cache: 'no-store'});
        if (!response.ok) throw new Error('Image unavailable');
        const blob = await response.blob();
        if (blob.type !== 'image/png' || !blob.size) throw new Error('Invalid image');
        imageFile = new File([blob], agentShare.dataset.filename, {type: 'image/png'});
        fileShareSupported = shareSupported && navigator.canShare({files: [imageFile]});
      } catch (_) {
        imageError = '房态图片加载失败，请刷新后重试，或点击“保存房态图片”。';
        status.textContent = imageError;
      } finally {
        agentShare.disabled = false;
        copyImage.disabled = false;
      }
    };
    agentShare.addEventListener('click', async () => {
      if (!imageFile || !fileShareSupported) { fallback(); return; }
      agentShare.disabled = true;
      try {
        await navigator.share({files: [imageFile], title: '悦山公寓房态'});
        // The browser can finish before the target app has actually sent the image.
        status.textContent = '已交给系统分享，请确认微信是否收到。若显示共享失败，可改用“复制房态图片”后粘贴，或保存图片发送。';
      } catch (error) {
        status.textContent = error.name === 'AbortError'
          ? '分享已取消或未完成，可重新尝试、复制图片或保存后发送。'
          : '系统未能完成图片分享，可改用“复制房态图片”后粘贴，或保存图片发送。';
      } finally {
        agentShare.disabled = false;
      }
    });
    copyImage.addEventListener('click', async () => {
      if (!imageFile) { fallback(); return; }
      copyImage.disabled = true;
      try {
        await navigator.clipboard.write([new ClipboardItem({'image/png': imageFile})]);
        status.textContent = '房态图片已复制。请打开微信聊天框按 Ctrl+V（Mac 使用 Command+V）粘贴，核对后发送。';
      } catch (_) {
        status.textContent = '浏览器未允许复制图片。可右键下方图片选择“复制图片”，或保存图片后在微信发送。';
      } finally {
        copyImage.disabled = false;
      }
    });
    if (shareSupported || copySupported) prepareImage();
  }
})();
