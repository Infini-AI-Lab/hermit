(() => {
  'use strict';
  const options = [
    { id: '01', name: 'AstraFlow · 细线实心', note: '2.5px · dataHead 的实心三角形', source: true },
    { id: '02', name: 'AstraFlow · 圆角开口', note: '5px · cycHead 的圆角开口', source: true },
    { id: '03', name: '轻量实心', note: '1.75px · 小三角形' },
    { id: '04', name: '开放箭头', note: '3px · 小开口箭头' },
    { id: '05', name: '一体箭头', note: '单一轮廓 · 细杆与箭头连成一体' },
  ];
  function render(id, start, tip, y, prefix) {
    const color = '#2C4A6B';
    if (id === '01') {
      const marker = `${prefix}-solid`;
      return `<defs><marker id="${marker}" viewBox="0 0 10 10" markerWidth="8" markerHeight="8" refX="8" refY="5" orient="auto"><path d="M1 1L9 5L1 9Z" fill="${color}"/></marker></defs><path d="M${start} ${y}H${tip - 2.5}" fill="none" stroke="${color}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" marker-end="url(#${marker})"/>`;
    }
    if (id === '02') {
      const marker = `${prefix}-open`;
      // The shaft stops behind the chevron rather than extending through its tip.
      return `<defs><marker id="${marker}" viewBox="0 0 10 10" markerWidth="4" markerHeight="4" refX="8" refY="5" orient="auto"><path d="M1 1L9 5L1 9" fill="none" stroke="#1F3554" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/></marker></defs><path d="M${start} ${y}H${tip - 2}" fill="none" stroke="#1F3554" stroke-width="5" stroke-linecap="round" stroke-linejoin="round" marker-end="url(#${marker})"/>`;
    }
    if (id === '03') {
      return `<path d="M${start} ${y}H${tip - 6}" fill="none" stroke="${color}" stroke-width="1.75" stroke-linecap="round"/><path d="M${tip - 9} ${y - 3.5}L${tip} ${y}L${tip - 9} ${y + 3.5}Z" fill="${color}"/>`;
    }
    if (id === '04') {
      return `<path d="M${start} ${y}H${tip - 1.5}" fill="none" stroke="${color}" stroke-width="3" stroke-linecap="round"/><path d="M${tip - 9} ${y - 5}L${tip} ${y}L${tip - 9} ${y + 5}" fill="none" stroke="${color}" stroke-width="3" stroke-linecap="round" stroke-linejoin="round"/>`;
    }
    return `<path d="M${start} ${y - 1.5}H${tip - 11}V${y - 6}L${tip} ${y}L${tip - 11} ${y + 6}V${y + 1.5}H${start}Z" fill="${color}"/>`;
  }
  window.AdapterArrowStyles = { options, render };
})();
