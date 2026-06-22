"""语义 DOM 遍历脚本 (浏览器 evaluate 注入)."""
from __future__ import annotations

V3_LOCATE_SCRIPT = """

() => {
    // 生成元素的 XPath
    function getXPath(element) {
        if (element === document.documentElement) {
            return '/html';
        }
        if (element === document.body) {
            return '/html/body';
        }
        if (element.id && element.id !== '') {
            return '//*[@id="' + element.id.replace(/"/g, '\\"') + '"]';
        }

        if (!element.parentNode || !element.parentNode.childNodes) {
            return '';
        }
        let ix = 0;
        const siblings = element.parentNode.childNodes;
        for (let i = 0; i < siblings.length; i++) {
            const sibling = siblings[i];
            if (sibling === element) {
                return getXPath(element.parentNode) + '/' + element.tagName.toLowerCase() + '[' + (ix + 1) + ']';
            }
            if (sibling.nodeType === 1 && sibling.tagName === element.tagName) {
                ix++;
            }
        }
    }

    /**
     * 检查元素是否对 UI 自动化有意义.
     *
     * 可见性规则 (关键):
     * - UI 自动化的目标是模拟用户操作, 用户只能看到和操作可见元素.
     * - 弹窗遮挡下的背景页元素、display:none 的隐藏菜单等都不应出现在快照中,
     *   否则 LLM 可能会选到不可操作的元素导致失败.
     * - 例外: 下拉选项(option)在收起时不可见, 但它们由专项脚本补充提取,
     *   不在本函数中处理.
     */
    function isRelevantElement(element) {
        // 跳过 script, style, meta 等无意义元素
        const skipTags = ['SCRIPT', 'STYLE', 'META', 'LINK', 'NOSCRIPT', 'HEAD'];
        if (skipTags.includes(element.tagName)) {
            return false;
        }

        // 可见性检查: 过滤不可见元素 (弹窗遮挡下的背景元素、隐藏菜单等)
        // 注意: 这里只检查元素自身, 不检查祖先, 因为祖先不可见时子元素也不会被遍历到
        if (!isVisible(element)) {
            return false;
        }

        // 可交互元素
        const interactiveTags = [
            'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'A',
            'LABEL', 'SUMMARY', 'DETAILS', 'MENU', 'MENUITEM', 'SPAN',
            'EL-CHECKBOX__INNER'
        ];
        if (interactiveTags.includes(element.tagName)) {
            return true;
        }

        // 表格及表格容器（Element UI el-table / Ant Design ant-table 等）
        if (element.tagName === 'TABLE' || element.tagName === 'TH') return true;
        if (element.tagName === 'DIV') {
            const cn = (element.className && typeof element.className === 'string') ? element.className : '';
            if (/el-table|ant-table|\\btable\\b/i.test(cn)) return true;
        }

        // 有 data-testid 的元素
        if (element.hasAttribute('data-testid')) {
            return true;
        }

        // 有 ARIA 属性的元素
        const ariaAttrs = ['aria-label', 'aria-labelledby', 'aria-describedby', 'role'];
        for (const attr of ariaAttrs) {
            if (element.hasAttribute(attr)) {
                return true;
            }
        }

        // 无文本但可点击的容器（如 create-button / icon button）
        // 仅在 div/span 上启用，避免放大噪音到所有标签
        if (element.tagName === 'DIV' || element.tagName === 'SPAN') {
            const cn = (element.className && typeof element.className === 'string') ? element.className : '';
            if (/(^|\\s)(create|new|add|plus|btn|button|action|operate|toolbar|icon)(-|_|\\b)/i.test(cn)) {
                return true;
            }
        }

        // 有可见文本的元素（排除空白文本和过长文本）
        const text = element.innerText || element.textContent || '';
        const trimmedText = text.trim();
        if (trimmedText.length > 0 && trimmedText.length <= 500) {
            // 只考虑文本长度合理的元素（避免提取整个页面文本）
            const decorativeTags = ['DIV', 'SPAN'];
            if (decorativeTags.includes(element.tagName)) {
                // 对于 div/span：有 id / data-testid / role 则提取
                if (element.id || element.hasAttribute('data-testid') || element.getAttribute('role')) {
                    return true;
                }
                // 有语义化 class（如卡片标题、列表项、开关等）的 div/span 也提取
                const cn = (element.className && typeof element.className === 'string') ? element.className : '';
                const semanticClassPattern = /title|name|header|card|selectable|label|content|item|desc|text|trigger|link|switch/i;
                if (semanticClassPattern.test(cn)) {
                    return true;
                }
                return false;
            }
            return true;
        }

        return false;
    }

    /**
     * 检查元素是否可见 (用户能看到的).
     *
     * 判断标准:
     * 1. offsetParent === null → display:none 或被脱离布局流隐藏
     * 2. getComputedStyle display === 'none'
     * 3. getComputedStyle visibility === 'hidden'
     * 4. opacity === 0 → 完全透明, 用户看不到
     * 5. getBoundingClientRect 宽高为 0 → 不占空间
     */
    function isVisible(element) {
        if (element.offsetParent === null && element.tagName !== 'BODY') {
            return false;
        }
        try {
            const style = window.getComputedStyle(element);
            if (style.display === 'none') return false;
            if (style.visibility === 'hidden') return false;
            if (parseFloat(style.opacity) === 0) return false;
        } catch (e) {}
        try {
            const rect = element.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) return false;
        } catch (e) {}
        return true;
    }

    // 提取元素的文本（截断）
    function extractText(element) {
        let text = element.innerText || element.textContent || '';
        text = text.trim();
        
        // 如果文本太长，截断
        if (text.length > 200) {
            text = text.substring(0, 200) + '...';
        }
        
        return text;
    }
    
    // 提取 ARIA 相关属性
    function extractAria(element) {
        const aria = {};
        const ariaAttrs = [
            'aria-label', 'aria-labelledby', 'aria-describedby',
            'aria-expanded', 'aria-hidden', 'aria-disabled',
            'aria-required', 'aria-checked', 'aria-selected'
        ];
        
        for (const attr of ariaAttrs) {
            const value = element.getAttribute(attr);
            if (value !== null) {
                aria[attr] = value;
            }
        }
        
        return Object.keys(aria).length > 0 ? aria : null;
    }
    
    // 提取元素信息
    function extractElementInfo(element) {
        // 必需字段
        const info = {
            tag: element.tagName.toLowerCase(),
            text: extractText(element),
            aria: null,
            role: null,
            testId: null,
            id: element.id || null,
            placeholder: element.getAttribute('placeholder') || null,
            name: element.getAttribute('name') || null,
            class: element.className || null
        };
        
        // 生成 XPath 候选（标记 confidence）
        const xpath = getXPath(element);
        info.xpath_candidate = xpath;
        // 如果有 id，confidence 为 high，否则为 low
        info.xpath_confidence = element.id ? 'high' : 'low';
        
        // ARIA role
        const role = element.getAttribute('role') || element.getAttribute('aria-role');
        if (role) {
            info.role = role;
        }
        
        // data-testid
        const testId = element.getAttribute('data-testid');
        if (testId) {
            info.testId = testId;
        }

        // input 的 type / value / 只读
        if (element.tagName === 'INPUT' || element.tagName === 'TEXTAREA') {
            info.type = (element.getAttribute('type') || 'text').toLowerCase();
            info.readOnly = element.hasAttribute('readonly') || !!element.readOnly;
            info.value = (element.value || '').trim().slice(0, 80);
        }
        if (element.getAttribute('aria-haspopup')) {
            info.haspopup = element.getAttribute('aria-haspopup');
        }

        // 计算 z-index：自身有数值则用自身的，否则向上查找第一个有数值 z-index 的祖先（弹窗/抽屉容器）
        try {
            let node = element;
            let zIndex = null;
            while (node && node !== document.body) {
                const style = window.getComputedStyle(node);
                if (style && style.zIndex && style.zIndex !== 'auto') {
                    zIndex = style.zIndex;
                    break;
                }
                node = node.parentElement;
            }
            info.zIndex = zIndex;
        } catch (e) {
            info.zIndex = null;
        }
        
        // ARIA 属性
        const aria = extractAria(element);
        if (aria) {
            info.aria = aria;
            // 提取 aria-label 到顶层，方便使用
            if (aria['aria-label']) {
                info['aria-label'] = aria['aria-label'];
            }
        }
        
        // 检查是否在弹窗/抽屉/overlay 内（向上遍历祖先）
        let node = element;
        while (node && node !== document.body) {
            var c = (node.className && typeof node.className === 'string') ? node.className : '';
            if (/dialog|modal|overlay|drawer|el-overlay|el-drawer/i.test(c)) {
                info.in_dialog = true;
                break;
            }
            node = node.parentElement;
        }

        // 是否在表单内
        node = element;
        while (node && node !== document.body) {
            var c2 = (node.className && typeof node.className === 'string') ? node.className : '';
            if (node.tagName === 'FORM' || /\bel-form\b|\bant-form\b/i.test(c2)) {
                info.in_form = true;
                break;
            }
            node = node.parentElement;
        }
        try {
            let p = element.parentElement;
            while (p && p !== document.body) {
                if (p.id) { info._parentId = p.id; break; }
                p = p.parentElement;
            }
        } catch (e) {}
        
        return info;
    }
    
    // 卡片容器 div 本身不触发点击，只有内部的 title span 可点击；跳过容器，只保留 span
    function isCardContainerWithClickableTitle(element) {
        if (element.tagName !== 'DIV') return false;
        const cn = (element.className && typeof element.className === 'string') ? element.className : '';
        if (!/card-selectable|knowledge-child__card/i.test(cn)) return false;
        const title = element.querySelector('span.card-selectable__header-right-title, [class*="header-right-title"]');
        return !!title;
    }
    
    // 遍历 DOM 树，提取有意义的元素
    const result = [];
    const visited = new Set(); // 避免重复提取
    
    function traverse(element) {
        if (!element || visited.has(element)) {
            return;
        }
        
        visited.add(element);
        
        // 检查当前元素
        if (isRelevantElement(element)) {
            if (!isCardContainerWithClickableTitle(element)) {
                const info = extractElementInfo(element);
                result.push(info);
            }
        }
        
        // 遍历子元素
        const children = element.children || [];
        for (let i = 0; i < children.length; i++) {
            traverse(children[i]);
        }

        // 遍历 Shadow DOM 子元素
        if (element.shadowRoot) {
            const srChildren = element.shadowRoot.children || [];
            for (let j = 0; j < srChildren.length; j++) {
                traverse(srChildren[j]);
            }
        }
    }
    
    // 从 body 开始遍历
    if (document.body) {
        traverse(document.body);
    }
    
    return result;
}
"""

V3_POST_VERIFY_SCRIPT = """

() => {
    // 生成元素的 XPath
    function getXPath(element) {
        if (element === document.documentElement) {
            return '/html';
        }
        if (element === document.body) {
            return '/html/body';
        }
        if (element.id && element.id !== '') {
            return '//*[@id="' + element.id.replace(/"/g, '\\"') + '"]';
        }

        if (!element.parentNode || !element.parentNode.childNodes) {
            return '';
        }
        let ix = 0;
        const siblings = element.parentNode.childNodes;
        for (let i = 0; i < siblings.length; i++) {
            const sibling = siblings[i];
            if (sibling === element) {
                return getXPath(element.parentNode) + '/' + element.tagName.toLowerCase() + '[' + (ix + 1) + ']';
            }
            if (sibling.nodeType === 1 && sibling.tagName === element.tagName) {
                ix++;
            }
        }
    }

    /**
     * 检查元素是否对 UI 自动化有意义.
     *
     * 可见性规则 (关键):
     * - UI 自动化的目标是模拟用户操作, 用户只能看到和操作可见元素.
     * - 弹窗遮挡下的背景页元素、display:none 的隐藏菜单等都不应出现在快照中,
     *   否则 LLM 可能会选到不可操作的元素导致失败.
     * - 例外: 下拉选项(option)在收起时不可见, 但它们由专项脚本补充提取,
     *   不在本函数中处理.
     */
    function isRelevantElement(element) {
        // 跳过 script, style, meta 等无意义元素
        const skipTags = ['SCRIPT', 'STYLE', 'META', 'LINK', 'NOSCRIPT', 'HEAD'];
        if (skipTags.includes(element.tagName)) {
            return false;
        }

        // 可见性检查: 过滤不可见元素 (弹窗遮挡下的背景元素、隐藏菜单等)
        // 注意: 这里只检查元素自身, 不检查祖先, 因为祖先不可见时子元素也不会被遍历到
        if (!isVisible(element)) {
            return false;
        }

        // 可交互元素
        const interactiveTags = [
            'BUTTON', 'INPUT', 'SELECT', 'TEXTAREA', 'A',
            'LABEL', 'SUMMARY', 'DETAILS', 'MENU', 'MENUITEM', 'SPAN',
            'EL-CHECKBOX__INNER'
        ];
        if (interactiveTags.includes(element.tagName)) {
            return true;
        }

        // 表格及表格容器（Element UI el-table / Ant Design ant-table 等）
        if (element.tagName === 'TABLE' || element.tagName === 'TH') return true;
        if (element.tagName === 'DIV') {
            const cn = (element.className && typeof element.className === 'string') ? element.className : '';
            if (/el-table|ant-table|\\btable\\b/i.test(cn)) return true;
        }

        // 有 data-testid 的元素
        if (element.hasAttribute('data-testid')) {
            return true;
        }

        // 有 ARIA 属性的元素
        const ariaAttrs = ['aria-label', 'aria-labelledby', 'aria-describedby', 'role'];
        for (const attr of ariaAttrs) {
            if (element.hasAttribute(attr)) {
                return true;
            }
        }

        // 无文本但可点击的容器（如 create-button / icon button）
        // 仅在 div/span 上启用，避免放大噪音到所有标签
        if (element.tagName === 'DIV' || element.tagName === 'SPAN') {
            const cn = (element.className && typeof element.className === 'string') ? element.className : '';
            if (/(^|\\s)(create|new|add|plus|btn|button|action|operate|toolbar|icon)(-|_|\\b)/i.test(cn)) {
                return true;
            }
        }

        // 有可见文本的元素（排除空白文本和过长文本）
        const text = element.innerText || element.textContent || '';
        const trimmedText = text.trim();
        if (trimmedText.length > 0 && trimmedText.length <= 500) {
            // 只考虑文本长度合理的元素（避免提取整个页面文本）
            const decorativeTags = ['DIV', 'SPAN'];
            if (decorativeTags.includes(element.tagName)) {
                // 对于 div/span：有 id / data-testid / role 则提取
                if (element.id || element.hasAttribute('data-testid') || element.getAttribute('role')) {
                    return true;
                }
                // 有语义化 class（如卡片标题、列表项、开关等）的 div/span 也提取
                const cn = (element.className && typeof element.className === 'string') ? element.className : '';
                const semanticClassPattern = /title|name|header|card|selectable|label|content|item|desc|text|trigger|link|switch/i;
                if (semanticClassPattern.test(cn)) {
                    return true;
                }
                return false;
            }
            return true;
        }

        return false;
    }

    /**
     * 检查元素是否可见 (用户能看到的).
     *
     * 判断标准:
     * 1. offsetParent === null → display:none 或被脱离布局流隐藏
     * 2. getComputedStyle display === 'none'
     * 3. getComputedStyle visibility === 'hidden'
     * 4. opacity === 0 → 完全透明, 用户看不到
     * 5. getBoundingClientRect 宽高为 0 → 不占空间
     */
    function isVisible(element) {
        if (element.offsetParent === null && element.tagName !== 'BODY') {
            return false;
        }
        try {
            const style = window.getComputedStyle(element);
            if (style.display === 'none') return false;
            if (style.visibility === 'hidden') return false;
            if (parseFloat(style.opacity) === 0) return false;
        } catch (e) {}
        try {
            const rect = element.getBoundingClientRect();
            if (rect.width === 0 && rect.height === 0) return false;
        } catch (e) {}
        return true;
    }

    // 提取元素的文本（截断）
    function extractText(element) {
        let text = element.innerText || element.textContent || '';
        text = text.trim();
        
        // 如果文本太长，截断
        if (text.length > 200) {
            text = text.substring(0, 200) + '...';
        }
        
        return text;
    }
    
    // 提取 ARIA 相关属性
    function extractAria(element) {
        const aria = {};
        const ariaAttrs = [
            'aria-label', 'aria-labelledby', 'aria-describedby',
            'aria-expanded', 'aria-hidden', 'aria-disabled',
            'aria-required', 'aria-checked', 'aria-selected'
        ];
        
        for (const attr of ariaAttrs) {
            const value = element.getAttribute(attr);
            if (value !== null) {
                aria[attr] = value;
            }
        }
        
        return Object.keys(aria).length > 0 ? aria : null;
    }
    
    // 提取元素信息
    function extractElementInfo(element) {
        // 必需字段
        const info = {
            tag: element.tagName.toLowerCase(),
            text: extractText(element),
            aria: null,
            role: null,
            testId: null,
            id: element.id || null,
            placeholder: element.getAttribute('placeholder') || null,
            name: element.getAttribute('name') || null,
            class: element.className || null
        };
        
        // 生成 XPath 候选（标记 confidence）
        const xpath = getXPath(element);
        info.xpath_candidate = xpath;
        // 如果有 id，confidence 为 high，否则为 low
        info.xpath_confidence = element.id ? 'high' : 'low';
        
        // ARIA role
        const role = element.getAttribute('role') || element.getAttribute('aria-role');
        if (role) {
            info.role = role;
        }
        
        // data-testid
        const testId = element.getAttribute('data-testid');
        if (testId) {
            info.testId = testId;
        }

        // input 的 type / value / 只读
        if (element.tagName === 'INPUT' || element.tagName === 'TEXTAREA') {
            info.type = (element.getAttribute('type') || 'text').toLowerCase();
            info.readOnly = element.hasAttribute('readonly') || !!element.readOnly;
            info.value = (element.value || '').trim().slice(0, 80);
        }
        if (element.getAttribute('aria-haspopup')) {
            info.haspopup = element.getAttribute('aria-haspopup');
        }

        // 计算 z-index：自身有数值则用自身的，否则向上查找第一个有数值 z-index 的祖先（弹窗/抽屉容器）
        try {
            let node = element;
            let zIndex = null;
            while (node && node !== document.body) {
                const style = window.getComputedStyle(node);
                if (style && style.zIndex && style.zIndex !== 'auto') {
                    zIndex = style.zIndex;
                    break;
                }
                node = node.parentElement;
            }
            info.zIndex = zIndex;
        } catch (e) {
            info.zIndex = null;
        }
        
        // ARIA 属性
        const aria = extractAria(element);
        if (aria) {
            info.aria = aria;
            // 提取 aria-label 到顶层，方便使用
            if (aria['aria-label']) {
                info['aria-label'] = aria['aria-label'];
            }
        }
        
        // 检查是否在弹窗/抽屉/overlay 内（向上遍历祖先）
        let node = element;
        while (node && node !== document.body) {
            var c = (node.className && typeof node.className === 'string') ? node.className : '';
            if (/dialog|modal|overlay|drawer|el-overlay|el-drawer/i.test(c)) {
                info.in_dialog = true;
                break;
            }
            node = node.parentElement;
        }

        // 是否在表单内
        node = element;
        while (node && node !== document.body) {
            var c2 = (node.className && typeof node.className === 'string') ? node.className : '';
            if (node.tagName === 'FORM' || /\bel-form\b|\bant-form\b/i.test(c2)) {
                info.in_form = true;
                break;
            }
            node = node.parentElement;
        }
        try {
            let p = element.parentElement;
            while (p && p !== document.body) {
                if (p.id) { info._parentId = p.id; break; }
                p = p.parentElement;
            }
        } catch (e) {}
        
        return info;
    }
    
    // 卡片容器 div 本身不触发点击，只有内部的 title span 可点击；跳过容器，只保留 span
    function isCardContainerWithClickableTitle(element) {
        if (element.tagName !== 'DIV') return false;
        const cn = (element.className && typeof element.className === 'string') ? element.className : '';
        if (!/card-selectable|knowledge-child__card/i.test(cn)) return false;
        const title = element.querySelector('span.card-selectable__header-right-title, [class*="header-right-title"]');
        return !!title;
    }
    
    function shouldIncludeForPostVerify(element) {
        if (isRelevantElement(element)) {
            return true;
        }
        const skipPv = ['SCRIPT', 'STYLE', 'META', 'LINK', 'NOSCRIPT', 'HEAD', 'SVG', 'PATH'];
        if (skipPv.includes(element.tagName)) {
            return false;
        }
        try {
            const st = window.getComputedStyle(element);
            if (st.display === 'none' || st.visibility === 'hidden' || parseFloat(st.opacity) === 0) {
                return false;
            }
        } catch (ePv) {}
        const rolePv = (element.getAttribute('role') || '').toLowerCase();
        if (rolePv === 'alert' || rolePv === 'status' || rolePv === 'log') {
            return true;
        }
        if (element.hasAttribute('aria-live')) {
            return true;
        }
        const cnPv = (element.className && typeof element.className === 'string') ? element.className : '';
        if (/message|toast|notification|notify|alert|error|fail|warning|tip|feedback|snackbar|banner|invalid|danger|success|form-error|help-text|desc|exception|hint/i.test(cnPv)) {
            return true;
        }
        const textyPv = ['P', 'LI', 'TD', 'TH', 'H1', 'H2', 'H3', 'H4', 'H5', 'H6', 'PRE', 'FIGCAPTION', 'BLOCKQUOTE', 'DD', 'DT', 'SECTION', 'ASIDE', 'MAIN', 'MARK', 'INS', 'DEL'];
        const tPv = (element.innerText || element.textContent || '').trim();
        if (textyPv.includes(element.tagName) && tPv.length >= 1 && tPv.length <= 600) {
            return true;
        }
        if ((element.tagName === 'DIV' || element.tagName === 'SPAN') && tPv.length >= 1 && tPv.length <= 320 && element.children.length === 0) {
            return true;
        }
        return false;
    }
    // 遍历 DOM 树，提取有意义的元素
    const result = [];
    const visited = new Set(); // 避免重复提取
    
    function traverse(element) {
        if (!element || visited.has(element)) {
            return;
        }
        
        visited.add(element);
        
        // 检查当前元素
        if (shouldIncludeForPostVerify(element)) {
            if (!isCardContainerWithClickableTitle(element)) {
                const info = extractElementInfo(element);
                result.push(info);
            }
        }
        
        // 遍历子元素
        const children = element.children || [];
        for (let i = 0; i < children.length; i++) {
            traverse(children[i]);
        }

        // 遍历 Shadow DOM 子元素
        if (element.shadowRoot) {
            const srChildren = element.shadowRoot.children || [];
            for (let j = 0; j < srChildren.length; j++) {
                traverse(srChildren[j]);
            }
        }
    }
    
    // 从 body 开始遍历
    if (document.body) {
        traverse(document.body);
    }
    
    return result;
}
"""
