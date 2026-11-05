"""浏览器自动化指纹隐匿与人类生物力学行为模拟（Playwright Sync API）。

移植并适配自 X_Crawler/stealth.py：
1. STEALTH_INIT_SCRIPT：抹除 navigator.webdriver、伪造 languages、补全 window.chrome.runtime、修复 permissions.query。
2. _MOUSE_POS：跨操作记忆每个标签页上次鼠标停留位置，杜绝从 (0, 0) 瞬移。
3. human_move_and_click：双控制点三阶贝塞尔曲线 + Ease-Out 减速瞄准 + 生理微颤抖 + 长距离偶发过冲（Overshoot）+ 真实按压时序。
4. human_scroll_to_element / human_page_scan_scroll：全程使用真实 mouse.wheel() 分档变速滚动 + 手部微漂移 + 偶发回拨微调。
5. human_idle_reading_gesture / human_reset_gaze：阅读指读滑移与操作后视线复位。
"""

from __future__ import annotations

import math
import random
import weakref
from typing import Any

from playwright.sync_api import Error as PlaywrightError, Locator, Page

# 记录每个页面（标签页）当前鼠标所在的坐标，使用 WeakKeyDictionary 避免已关闭标签页残留引用
_MOUSE_POS: weakref.WeakKeyDictionary[Page, tuple[float, float]] = weakref.WeakKeyDictionary()

# 深度隐藏浏览器自动化指纹的初始化 JS 补丁
STEALTH_INIT_SCRIPT = """
(() => {
    // 1. 彻底抹除 navigator.webdriver 标记
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

    // 2. 伪造正常的语言列表特征
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en', 'zh-CN', 'zh'] });

    // 3. 确保 window.chrome 对象完整（模拟正常安装的 Chrome 扩展运行环境）
    if (!window.chrome) {
        window.chrome = {};
    }
    if (!window.chrome.runtime) {
        window.chrome.runtime = {
            PlatformOs: { MAC: 'mac', WIN: 'win', ANDROID: 'android', CROS: 'cros', LINUX: 'linux', OPENBSD: 'openbsd' },
            PlatformArch: { ARM: 'arm', X86_32: 'x86-32', X86_64: 'x86-64' }
        };
    }

    // 4. 修复自动化环境下 Notification 权限查询异常
    if (window.navigator.permissions && window.navigator.permissions.query) {
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications'
                ? Promise.resolve({ state: Notification.permission })
                : originalQuery(parameters)
        );
    }
})();
"""


def _unwrap_page(driver_or_page: Any) -> Page:
    """兼容 BrowserSession 包装对象与原生 Playwright Page。"""
    return getattr(driver_or_page, "page", driver_or_page)


def human_wait(driver_or_page: Any, min_ms: int, max_ms: int) -> None:
    """随机暂停等待，保持 Playwright 事件循环活跃。"""
    page = _unwrap_page(driver_or_page)
    wait_ms = random.randint(min_ms, max(min_ms, max_ms))
    page.wait_for_timeout(wait_ms)


def human_reset_gaze(driver_or_page: Any) -> None:
    """将鼠标平滑移回页面中上方阅读准备区并更新 _MOUSE_POS。"""
    page = _unwrap_page(driver_or_page)
    viewport = page.viewport_size or {"width": 1600, "height": 1200}
    vw = viewport["width"]
    vh = viewport["height"]
    reset_x = random.uniform(vw * 0.35, vw * 0.60)
    reset_y = random.uniform(vh * 0.25, vh * 0.45)
    try:
        page.mouse.move(reset_x, reset_y, steps=random.randint(12, 22))
        _MOUSE_POS[page] = (reset_x, reset_y)
    except PlaywrightError:
        pass


def human_idle_reading_gesture(
    driver_or_page: Any,
    card_locator: Locator | None = None,
    min_pause_ms: int = 180,
    max_pause_ms: int = 480,
) -> None:
    """模拟真人在浏览页面/详情字段时的无意识手部指读滑移与短暂停顿。"""
    page = _unwrap_page(driver_or_page)
    box = None
    if card_locator is not None:
        try:
            box = card_locator.bounding_box(timeout=1_500)
        except Exception:
            box = None

    viewport = page.viewport_size or {"width": 1600, "height": 1200}
    vw, vh = viewport["width"], viewport["height"]

    if random.random() < 0.78:
        start_x, start_y = _MOUSE_POS.get(page, (vw * 0.45, vh * 0.35))
        if box and box["width"] > 20 and box["height"] > 10:
            read_x = box["x"] + box["width"] * random.uniform(0.18, 0.58)
            read_y = box["y"] + min(box["height"], 400) * random.uniform(0.25, 0.72)
        else:
            read_x = random.uniform(vw * 0.25, vw * 0.65)
            read_y = random.uniform(vh * 0.28, vh * 0.62)

        steps = random.randint(8, 16)
        try:
            for i in range(1, steps + 1):
                t = i / steps
                cur_x = start_x + (read_x - start_x) * t + random.gauss(0, 0.9)
                cur_y = start_y + (read_y - start_y) * t + random.gauss(0, 0.9)
                page.mouse.move(cur_x, cur_y)
                page.wait_for_timeout(random.randint(5, 12))
            _MOUSE_POS[page] = (read_x, read_y)
        except PlaywrightError:
            return

    if max_pause_ms > 0:
        try:
            page.wait_for_timeout(random.randint(min_pause_ms, max(min_pause_ms, max_pause_ms)))
        except PlaywrightError:
            pass


def human_page_scan_scroll(driver_or_page: Any, target_distance: float = 420.0) -> float:
    """模拟真人在列表页用鼠标滚轮小幅变速下拉扫视，替代静止死等。"""
    page = _unwrap_page(driver_or_page)
    viewport = page.viewport_size or {"width": 1600, "height": 1200}
    vw, vh = viewport["width"], viewport["height"]

    cur_x, cur_y = _MOUSE_POS.get(
        page, (random.uniform(vw * 0.35, vw * 0.62), random.uniform(vh * 0.30, vh * 0.58))
    )
    if page not in _MOUSE_POS or cur_y > vh * 0.82 or cur_y < vh * 0.15:
        cur_x = random.uniform(vw * 0.35, vw * 0.62)
        cur_y = random.uniform(vh * 0.32, vh * 0.58)
        try:
            page.mouse.move(cur_x, cur_y, steps=random.randint(8, 14))
            _MOUSE_POS[page] = (cur_x, cur_y)
        except PlaywrightError:
            return 0.0

    goal = target_distance * random.uniform(0.8, 1.2)
    total_scrolled = 0.0
    flick_count = 0

    try:
        while total_scrolled < goal:
            remaining = goal - total_scrolled
            if remaining > 220:
                step_scroll = random.uniform(95, 165)
                pause_ms = random.randint(25, 55)
            else:
                step_scroll = max(40.0, remaining * random.uniform(0.65, 1.0))
                pause_ms = random.randint(40, 85)

            if random.random() < 0.60:
                cur_x = max(120.0, min(vw - 120.0, cur_x + random.gauss(0, 1.5)))
                cur_y = max(120.0, min(vh - 120.0, cur_y + random.gauss(0, 1.1)))
                page.mouse.move(cur_x, cur_y)
                _MOUSE_POS[page] = (cur_x, cur_y)

            page.mouse.wheel(0, step_scroll)
            total_scrolled += step_scroll
            flick_count += 1
            page.wait_for_timeout(pause_ms)

        if flick_count > 0 and random.random() < 0.25:
            tiny_over = random.uniform(20, 45)
            page.mouse.wheel(0, tiny_over)
            page.wait_for_timeout(random.randint(50, 110))
            back_delta = -tiny_over * random.uniform(0.75, 1.15)
            page.mouse.wheel(0, back_delta)
            total_scrolled += tiny_over + back_delta
            page.wait_for_timeout(random.randint(40, 90))
    except PlaywrightError:
        pass

    return total_scrolled


def human_scroll_to_element(driver_or_page: Any, locator: Locator) -> None:
    """高拟真度模拟真人滚动鼠标滚轮将目标元素滚入“黄金阅读视野”：
    1. 全程使用真实的 page.mouse.wheel() 触发滚轮事件。
    2. 目标位置设定在屏幕中上方（距离顶部 24%~42% 处），误差 <= 70px。
    3. 远/中/近三档变速拨动滚轮 + 60% 概率伴随手部联动微漂移。
    4. 25% 概率模拟“滚过头一点再往回拨一下”的真人视觉定位微调。
    """
    page = _unwrap_page(driver_or_page)
    viewport = page.viewport_size or {"width": 1600, "height": 1200}
    vh = viewport["height"]
    vw = viewport["width"]

    cur_x, cur_y = _MOUSE_POS.get(
        page, (random.uniform(vw * 0.35, vw * 0.65), random.uniform(vh * 0.3, vh * 0.6))
    )
    if page not in _MOUSE_POS or cur_y > vh * 0.82 or cur_y < vh * 0.12:
        cur_x = random.uniform(vw * 0.35, vw * 0.65)
        cur_y = random.uniform(vh * 0.32, vh * 0.58)
        try:
            page.mouse.move(cur_x, cur_y, steps=random.randint(10, 18))
            _MOUSE_POS[page] = (cur_x, cur_y)
        except PlaywrightError:
            return

    target_band_y = vh * random.uniform(0.24, 0.42)
    scroll_steps = 0

    for _ in range(25):
        try:
            box = locator.bounding_box(timeout=1_500)
        except Exception:
            box = None
        if not box:
            break

        diff_y = box["y"] - target_band_y
        if abs(diff_y) <= 70:
            break

        direction = 1 if diff_y > 0 else -1
        abs_diff = abs(diff_y)

        if abs_diff > 400:
            step_scroll = random.uniform(110, 180)
            pause_ms = random.randint(25, 55)
        elif abs_diff > 150:
            step_scroll = random.uniform(65, 115)
            pause_ms = random.randint(40, 85)
        else:
            step_scroll = min(abs_diff * random.uniform(0.6, 0.9), random.uniform(35, 70))
            pause_ms = random.randint(60, 120)

        try:
            if random.random() < 0.6:
                cur_x = max(100.0, min(vw - 100.0, cur_x + random.gauss(0, 1.6)))
                cur_y = max(100.0, min(vh - 100.0, cur_y + random.gauss(0, 1.2)))
                page.mouse.move(cur_x, cur_y)
                _MOUSE_POS[page] = (cur_x, cur_y)

            delta_y = direction * step_scroll
            page.mouse.wheel(0, delta_y)
            scroll_steps += 1
            page.wait_for_timeout(pause_ms)
        except PlaywrightError:
            break

    if scroll_steps > 0 and random.random() < 0.25:
        try:
            tiny_over = random.choice([-1, 1]) * random.uniform(20, 45)
            page.mouse.wheel(0, tiny_over)
            page.wait_for_timeout(random.randint(60, 130))
            page.mouse.wheel(0, -tiny_over * random.uniform(0.7, 1.1))
            page.wait_for_timeout(random.randint(50, 100))
        except PlaywrightError:
            pass

    # 若滚轮 25 步后目标元素仍不在可视区域内，使用原生 scroll_into_view_if_needed 兜底
    try:
        box = locator.bounding_box(timeout=1_500)
        if not box or box["y"] < 0 or box["y"] + box["height"] > vh:
            locator.scroll_into_view_if_needed(timeout=2_000)
    except Exception:
        pass


def human_move_and_click(
    driver_or_page: Any,
    locator: Locator,
    *,
    timeout: float = 5_000,
    scroll_if_needed: bool = True,
) -> None:
    """高拟真度模拟真人鼠标轨迹并点击（融合 5 项生物力学特征）：
    1. 连续起点：记住上一次鼠标停留的位置 _MOUSE_POS，杜绝从 (0, 0) 瞬移。
    2. 弧线轨迹（三阶贝塞尔曲线）：双控制点偏移量随距离动态缩放。
    3. 变速运动（Ease-Out 减速瞄准）：t = 1 - math.pow(1 - raw_t, 2.2)。
    4. 生理微颤抖与偶发过冲（Jitter & Overshoot）：随进度衰减的微抖动，长距离(>120px) 30% 概率滑过头再拉回。
    5. 真实按压时长：悬停瞄准（80~220ms）+ 命中检测 + 手指按下保持（50~130ms）后松开。
    """
    page = _unwrap_page(driver_or_page)
    try:
        box = locator.bounding_box(timeout=1_500)
    except Exception:
        box = None

    viewport = page.viewport_size or {"width": 1600, "height": 1200}
    vw, vh = viewport["width"], viewport["height"]
    if scroll_if_needed and (not box or box["y"] < 60 or box["y"] > vh - 80):
        human_scroll_to_element(page, locator)
        try:
            box = locator.bounding_box(timeout=1_500)
        except Exception:
            box = None

    if box and (box["y"] < 0 or box["y"] + box["height"] > vh or box["x"] < 0 or box["x"] + box["width"] > vw):
        try:
            locator.scroll_into_view_if_needed(timeout=2_000)
            box = locator.bounding_box(timeout=1_500)
        except Exception:
            box = None

    if not box or box["width"] <= 0 or box["height"] <= 0:
        try:
            locator.click(timeout=timeout)
        except PlaywrightError:
            locator.evaluate("el => el.click()", timeout=timeout)
        return

    # 1. 获取连续起点坐标
    start_x, start_y = _MOUSE_POS.get(
        page, (random.uniform(300, 900), random.uniform(200, 600))
    )

    # 2. 按钮内部 30%~70% 随机落点
    target_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
    target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)

    dist = math.hypot(target_x - start_x, target_y - start_y)
    steps = max(14, min(45, int(dist / 14) + random.randint(-3, 5)))

    # 3. 双控制点三阶贝塞尔曲线
    offset_scale = max(20.0, dist * 0.25)
    ctrl1_x = start_x + (target_x - start_x) * random.uniform(0.2, 0.4) + random.uniform(-offset_scale, offset_scale)
    ctrl1_y = start_y + (target_y - start_y) * random.uniform(0.2, 0.4) + random.uniform(-offset_scale, offset_scale)
    ctrl2_x = start_x + (target_x - start_x) * random.uniform(0.6, 0.8) + random.uniform(-offset_scale, offset_scale)
    ctrl2_y = start_y + (target_y - start_y) * random.uniform(0.6, 0.8) + random.uniform(-offset_scale, offset_scale)

    # 4. 沿贝塞尔曲线分步移动鼠标（Ease-Out 减速 + 衰减生理微颤抖）
    for i in range(1, steps + 1):
        raw_t = i / steps
        t = 1 - math.pow(1 - raw_t, 2.2)

        u = 1 - t
        cur_x = (u**3) * start_x + 3 * (u**2) * t * ctrl1_x + 3 * u * (t**2) * ctrl2_x + (t**3) * target_x
        cur_y = (u**3) * start_y + 3 * (u**2) * t * ctrl1_y + 3 * u * (t**2) * ctrl2_y + (t**3) * target_y

        if i < steps:
            jitter = (1 - raw_t) * 1.8
            cur_x += random.gauss(0, jitter)
            cur_y += random.gauss(0, jitter)

        page.mouse.move(cur_x, cur_y)
        step_delay = random.uniform(3, 9) + (raw_t * 5)
        page.wait_for_timeout(step_delay)

    # 5. 长距离 (>120px) 30% 概率触发 Overshoot（滑过头再微调拉回）
    if dist > 120 and random.random() < 0.3:
        overshoot_x = target_x + random.uniform(-6, 6)
        overshoot_y = target_y + random.uniform(-5, 5)
        page.mouse.move(overshoot_x, overshoot_y, steps=random.randint(2, 4))
        page.wait_for_timeout(random.randint(25, 65))
        page.mouse.move(target_x, target_y, steps=random.randint(2, 4))

    # 6. 悬停瞄准（80~220ms）
    hover_ms = random.randint(80, 220)
    page.wait_for_timeout(hover_ms)
    _MOUSE_POS[page] = (target_x, target_y)

    # 7. 命中校验：确认 (target_x, target_y) 未被顶栏/浮层遮挡，否则降级使用 JS click 兜底
    try:
        hit_ok = locator.evaluate(
            "(el, pt) => { const hit = document.elementFromPoint(pt.x, pt.y); return Boolean(hit && (el === hit || el.contains(hit))); }",
            {"x": target_x, "y": target_y},
            timeout=1_500,
        )
    except PlaywrightError:
        hit_ok = False

    if hit_ok:
        hold_ms = random.randint(50, 130)
        page.mouse.down()
        page.wait_for_timeout(hold_ms)
        page.mouse.up()
    else:
        locator.evaluate("el => el.click()", timeout=timeout)
