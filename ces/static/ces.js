const menuToggle = document.querySelector('.menu-toggle');
const navigation = document.querySelector('#site-navigation');
document.querySelector('.notification-error[tabindex]')?.focus();

if (menuToggle && navigation) {
  menuToggle.addEventListener('click', () => {
    const isOpen = menuToggle.getAttribute('aria-expanded') === 'true';
    menuToggle.setAttribute('aria-expanded', String(!isOpen));
    menuToggle.setAttribute('aria-label', isOpen ? 'เปิดเมนู' : 'ปิดเมนู');
    navigation.classList.toggle('is-open', !isOpen);
  });
  navigation.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && menuToggle.getAttribute('aria-expanded') === 'true') {
      menuToggle.click();
      menuToggle.focus();
    }
  });
}

for (const form of document.querySelectorAll('[data-stock-form]')) {
  const itemSelect = form.querySelector('[data-item-select]');
  const unitSelect = form.querySelector('[data-unit-select]');
  const quantity = form.querySelector('[name="quantity"]');
  const loanInput = form.querySelector('[name="loan_id"]');
  const summary = form.querySelector('[data-stock-summary]');
  const before = form.querySelector('[data-stock-before]');
  const after = form.querySelector('[data-stock-after]');

  const updateUnit = () => {
    const selected = unitSelect?.selectedOptions[0];
    if (loanInput) loanInput.value = selected?.dataset.loan || '';
    const loanSummary = form.querySelector('[data-loan-summary]');
    if (loanSummary) {
      loanSummary.textContent = selected?.dataset.borrower || '';
      loanSummary.hidden = !selected?.dataset.borrower;
    }
    const statusSelect = form.querySelector('[name="status"]');
    if (statusSelect) {
      for (const option of statusSelect.options) {
        option.disabled = option.value === selected?.dataset.status;
      }
      if (statusSelect.selectedOptions[0]?.disabled) statusSelect.value = '';
    }
  };
  const updateItem = () => {
    const selected = itemSelect?.selectedOptions[0];
    const isTool = selected?.dataset.kind === 'tool';
    for (const group of form.querySelectorAll('[data-tool-fields], [data-part-fields]')) {
      const enabled = Boolean(selected?.value) && (group.hasAttribute('data-tool-fields') === isTool);
      group.hidden = !enabled;
      for (const control of group.querySelectorAll('input, select, textarea')) {
        control.disabled = !enabled;
      }
    }
    if (summary && before && after) {
      const available = BigInt(selected?.dataset.available || '0');
      const rawQuantity = quantity?.value || '';
      const change = isTool ? 1n : (/^[+-]?[0-9]+$/.test(rawQuantity) ? BigInt(rawQuantity) : null);
      const direction = form.dataset.stockForm === 'stock_out' ? -1n : 1n;
      before.textContent = available.toLocaleString('th-TH');
      after.textContent = change === null ? '—' : (available + change * direction).toLocaleString('th-TH');
      summary.hidden = !selected?.value;
    }
  };

  unitSelect?.addEventListener('change', updateUnit);
  itemSelect?.addEventListener('change', updateItem);
  quantity?.addEventListener('input', updateItem);
  updateUnit();
  updateItem();
}

for (const form of document.querySelectorAll('[data-item-form]')) {
  const kind = form.querySelector('[name="kind"]');
  const minimumField = form.querySelector('[data-minimum-field]');
  const minimum = minimumField.querySelector('input');
  const toggleMinimum = () => {
    const isTool = kind.value === 'tool';
    minimumField.hidden = isTool;
    minimum.disabled = isTool;
    minimum.required = !isTool;
  };
  kind.addEventListener('change', toggleMinimum);
  toggleMinimum();
}
