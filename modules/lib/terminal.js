/*!
 * terminal.js —— 终端风格红点视觉增强（所有功能模块共用）
 *
 * 作用：
 *   给左上角红点（关闭/返回首页按钮）加 hover/active 视觉反馈。
 *   点击行为已由原生 <a href="/"> 提供，键盘 Enter、Tab、右键新窗口均原生支持，
 *   故此处不再做任何 click 拦截或 location 跳转。
 *
 * 用法（放在页面底部，DOM 之后）：
 *   <script src="/modules/lib/terminal.js"></script>
 *
 * 兼容各模块里红点的不同 class 命名：
 *   .term-dot.r   hash / timestamp / zhconvert / currency-converter / fortune / notes / tetris
 *   .dot.r        price-calc
 *   .tdot.red     futures 行情看板
 */
(function () {
    'use strict';

    var DOT_SELECTOR = '.term-dot.r, .dot.r, .tdot.red, .dot.red, [data-close="home"]';

    function init() {
        if (!document.querySelector(DOT_SELECTOR)) return;

        // 视觉增强：cursor + hover 提亮 + active 按下
        var css = [
            DOT_SELECTOR + ' { cursor: pointer; transition: filter .15s ease, transform .15s ease; text-decoration: none; }',
            DOT_SELECTOR + ':hover { filter: brightness(1.3); transform: scale(1.15); }',
            DOT_SELECTOR + ':active { transform: scale(0.95); }'
        ].join('\n');
        var style = document.createElement('style');
        style.setAttribute('data-terminal-js', '1');
        style.textContent = css;
        document.head.appendChild(style);
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
