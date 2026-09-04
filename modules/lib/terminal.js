/*!
 * terminal.js —— 终端风格页面通用行为（所有功能模块共用）
 *
 * 作用：
 *   1. 左上角红色圆点 = 关闭窗口，点击返回首页 /
 *   2. 自动给红点补上 pointer 光标、hover 反馈与 title 提示
 *
 * 用法（放在页面底部，DOM 之后）：
 *   <script src="/modules/lib/terminal.js"></script>
 *
 * 兼容各模块里红点的不同 class 命名：
 *   .term-dot.r   hash / timestamp / zhconvert / currency-converter / fortune / notes
 *   .dot.r        price-calc
 *   .tdot.red     futures 行情看板
 */
(function () {
    'use strict';

    // 红点选择器：覆盖现有模块的三套命名，另留 data-close="home" 作为显式逃生口
    var DOT_SELECTOR = '.term-dot.r, .dot.r, .tdot.red, .dot.red, [data-close="home"]';

    function injectStyle() {
        var css = [
            DOT_SELECTOR + ' { cursor: pointer; transition: filter .15s ease, transform .15s ease; }',
            DOT_SELECTOR + ':hover { filter: brightness(1.3); transform: scale(1.15); }',
            DOT_SELECTOR + ':active { transform: scale(0.95); }'
        ].join('\n');
        var style = document.createElement('style');
        style.setAttribute('data-terminal-js', '1');
        style.textContent = css;
        document.head.appendChild(style);
    }

    function goHome() {
        window.location.href = '/';
    }

    function init() {
        injectStyle();
        var dots = document.querySelectorAll(DOT_SELECTOR);
        for (var i = 0; i < dots.length; i++) {
            (function (dot) {
                dot.setAttribute('title', '关闭（返回首页）');
                if (!dot.hasAttribute('role')) dot.setAttribute('role', 'button');
                if (dot.tabIndex < 0) dot.tabIndex = 0;
                dot.addEventListener('click', function (e) {
                    e.preventDefault();
                    goHome();
                });
                // 键盘可达：聚焦时回车 / 空格同样返回首页
                dot.addEventListener('keydown', function (e) {
                    if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        goHome();
                    }
                });
            })(dots[i]);
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})();
