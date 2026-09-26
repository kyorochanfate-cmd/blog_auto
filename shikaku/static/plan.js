(function () {
  'use strict';
  var $ = function (id) { return document.getElementById(id); };
  var data = [];
  var PHASES = [
    { name: 'インプット（テキストを1周）', share: 0.55 },
    { name: '問題演習（過去問・問題集）', share: 0.35 },
    { name: '直前対策（弱点の復習・模試）', share: 0.10 }
  ];

  function todayLocal() {
    var d = new Date();
    return new Date(d.getFullYear(), d.getMonth(), d.getDate());
  }
  function parseDate(s) {
    var p = s.split('-');
    return new Date(+p[0], +p[1] - 1, +p[2]);
  }
  function fmt(d) {
    return (d.getMonth() + 1) + '月' + d.getDate() + '日';
  }
  function addDays(d, n) {
    var x = new Date(d.getTime());
    x.setDate(x.getDate() + n);
    return x;
  }
  function esc(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function fillQuals() {
    var sel = $('qual');
    sel.innerHTML = '';
    data.forEach(function (q) {
      var o = document.createElement('option');
      o.value = q.slug;
      o.textContent = q.name + (q.exams.length ? '' : '（日程未発表）');
      sel.appendChild(o);
    });
    var want = new URLSearchParams(location.search).get('q');
    if (want && data.some(function (q) { return q.slug === want; })) sel.value = want;
  }

  function current() {
    var slug = $('qual').value;
    for (var i = 0; i < data.length; i++) if (data[i].slug === slug) return data[i];
    return null;
  }

  function fillExams() {
    var q = current();
    var sel = $('exam');
    sel.innerHTML = '';
    if (q && q.exams.length) {
      q.exams.forEach(function (e) {
        var o = document.createElement('option');
        o.value = e.date;
        o.textContent = e.date + (e.label ? '（' + e.label + '）' : '');
        sel.appendChild(o);
      });
      sel.disabled = false;
    } else {
      var o = document.createElement('option');
      o.textContent = '試験日が未発表です';
      sel.appendChild(o);
      sel.disabled = true;
    }
    var hint = $('hours-hint');
    if (q && q.hours && q.hours.min) {
      var h = q.hours;
      $('total').value = Math.round(((h.min || 0) + (h.max || h.min)) / 2);
      hint.textContent = '一般的な目安: ' + h.min + (h.max ? '〜' + h.max : '') + '時間' +
        (h.source ? '（出典: ' + h.source + '）' : '');
    } else {
      if (!$('total').value) $('total').value = 100;
      hint.textContent = 'この資格の勉強時間の目安は確認中です。ご自身の見込みを入力してください。';
    }
  }

  function render() {
    var out = $('result');
    var q = current();
    var total = parseFloat($('total').value);
    var ratio = parseFloat($('ratio').value) || 1;
    if (!q || !q.exams.length || !(total > 0)) { out.innerHTML = ''; return; }

    var start = todayLocal();
    var exam = parseDate($('exam').value);
    var days = Math.round((exam - start) / 86400000);
    if (days <= 0) { out.innerHTML = '<p>試験日を過ぎています。</p>'; return; }

    var weekdays = 0, weekends = 0;
    for (var i = 0; i < days; i++) {
      var w = addDays(start, i).getDay();
      if (w === 0 || w === 6) weekends++; else weekdays++;
    }
    var unit = total / (weekdays + weekends * ratio);
    var wd = unit, we = unit * ratio;

    var html = '<h2>計算結果</h2>';
    html += '<p>試験まで<strong>' + days + '日</strong>（平日' + weekdays + '日・休日' + weekends + '日）。</p>';
    html += '<p>合計' + total + '時間を達成するには、<strong>平日' + wd.toFixed(1) + '時間・休日' + we.toFixed(1) +
      '時間</strong>の勉強が必要です。</p>';
    if (wd > 4) {
      html += '<p class="warn">平日の負担がかなり大きい計画です。次回の試験に回すか、通信講座で効率を上げることも検討してください。</p>';
    }
    html += '<table class="tbl"><thead><tr><th>段階</th><th>期間</th><th>時間</th></tr></thead><tbody>';
    var cursor = start;
    PHASES.forEach(function (p, idx) {
      var n = idx === PHASES.length - 1 ? Math.round((exam - cursor) / 86400000) : Math.max(1, Math.round(days * p.share));
      var end = addDays(cursor, n - 1);
      html += '<tr><td>' + esc(p.name) + '</td><td>' + fmt(cursor) + '〜' + fmt(end) + '（' + n + '日）</td><td>約' +
        Math.round(total * p.share) + '時間</td></tr>';
      cursor = addDays(end, 1);
    });
    html += '</tbody></table>';
    html += '<p><a href="/q/' + encodeURIComponent(q.slug) + '/">' + esc(q.name) + 'の試験日程・合格率を見る</a></p>';
    out.innerHTML = html;
  }

  function onQual() { fillExams(); render(); }

  fetch('/static/plan-data.json')
    .then(function (r) { return r.json(); })
    .then(function (d) {
      data = d;
      fillQuals();
      fillExams();
      render();
      $('qual').addEventListener('change', onQual);
      ['exam', 'total', 'ratio'].forEach(function (id) {
        $(id).addEventListener('input', render);
        $(id).addEventListener('change', render);
      });
    });
})();
