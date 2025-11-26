import random
import time
import math
import logging
from typing import Union, List, Tuple
from playwright.sync_api import Page, Locator

logger = logging.getLogger(__name__)


class HumanActions:
    def __init__(self, page: Page):
        self.page = page

    def random_sleep(self, min_seconds: float = 0.5, max_seconds: float = 2.0):
        """Sleep for a random duration between min and max seconds."""
        duration = random.uniform(min_seconds, max_seconds)
        time.sleep(duration)

    def _get_cubic_bezier_path(
        self, start: Tuple[float, float], end: Tuple[float, float], steps: int = 20
    ) -> List[Tuple[float, float]]:
        """Generate points along a cubic Bezier curve to simulate natural mouse movement."""
        x1, y1 = start
        x2, y2 = end

        # Control points - introduce some randomness
        dist = math.hypot(x2 - x1, y2 - y1)

        # Control point 1
        cx1 = x1 + (x2 - x1) * 0.3 + random.uniform(-dist / 5, dist / 5)
        cy1 = y1 + (y2 - y1) * 0.3 + random.uniform(-dist / 5, dist / 5)

        # Control point 2
        cx2 = x1 + (x2 - x1) * 0.7 + random.uniform(-dist / 5, dist / 5)
        cy2 = y1 + (y2 - y1) * 0.7 + random.uniform(-dist / 5, dist / 5)

        path = []
        for i in range(steps + 1):
            t = i / steps
            # Bezier formula
            x = (
                (1 - t) ** 3 * x1
                + 3 * (1 - t) ** 2 * t * cx1
                + 3 * (1 - t) * t**2 * cx2
                + t**3 * x2
            )
            y = (
                (1 - t) ** 3 * y1
                + 3 * (1 - t) ** 2 * t * cy1
                + 3 * (1 - t) * t**2 * cy2
                + t**3 * y2
            )
            path.append((x, y))

        return path

    def move_mouse(self, x: float, y: float):
        """Move mouse to (x, y) following a natural curve."""
        try:
            start_pos = (
                self.page.mouse.position
                if hasattr(self.page.mouse, "position")
                else {"x": 0, "y": 0}
            )
            start_x, start_y = start_pos.get("x", 0), start_pos.get("y", 0)

            path = self._get_cubic_bezier_path((start_x, start_y), (x, y))

            for point_x, point_y in path:
                self.page.mouse.move(point_x, point_y)
                # Tiny sleep between movements for fluidity
                time.sleep(random.uniform(0.001, 0.01))

        except Exception as e:
            logger.error(f"Error moving mouse: {e}")
            # Fallback to direct move if something breaks
            self.page.mouse.move(x, y)

    def get_safe_point(self, box: dict) -> Tuple[float, float]:
        """
        Get a random point within a bounding box, biased towards the center.
        box should have x, y, width, height.
        """
        if not box:
            return 0, 0

        # Center
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2

        # Introduce standard deviation relative to size
        sigma_x = box["width"] / 6
        sigma_y = box["height"] / 6

        # Generate random point with gaussian distribution
        tx = random.gauss(cx, sigma_x)
        ty = random.gauss(cy, sigma_y)

        # Clamp to box boundaries (keeping a small margin)
        margin = 2
        tx = max(box["x"] + margin, min(box["x"] + box["width"] - margin, tx))
        ty = max(box["y"] + margin, min(box["y"] + box["height"] - margin, ty))

        return tx, ty

    def click(self, selector_or_locator: Union[str, Locator], timeout: int = 30000):
        """Human-like click on an element."""
        try:
            if isinstance(selector_or_locator, str):
                locator = self.page.locator(selector_or_locator).first
            else:
                locator = selector_or_locator

            locator.wait_for(state="visible", timeout=timeout)
            box = locator.bounding_box()

            if box:
                x, y = self.get_safe_point(box)
                self.move_mouse(x, y)
                self.random_sleep(0.2, 0.6)
                self.page.mouse.down()
                self.random_sleep(0.05, 0.15)  # Click duration
                self.page.mouse.up()
            else:
                # Fallback
                locator.click()

        except Exception as e:
            logger.error(f"Error executing human click: {e}")
            # Fallback
            if isinstance(selector_or_locator, str):
                self.page.click(selector_or_locator)
            else:
                selector_or_locator.click()

    def type(
        self,
        selector_or_locator: Union[str, Locator],
        text: str,
        delay_min: float = 0.05,
        delay_max: float = 0.2,
    ):
        """Type text with variable delays simulating human typing."""
        self.click(selector_or_locator)
        self.random_sleep(0.3, 0.8)

        for char in text:
            self.page.keyboard.type(char)
            self.random_sleep(delay_min, delay_max)

            # Occasional pause
            if random.random() < 0.05:
                self.random_sleep(0.5, 1.5)

    def random_hover(self):
        """Hover over a random interactive element on the page."""
        try:
            # Common interactive elements
            selectors = ["a", "button", "input", "[role='button']"]
            selector = random.choice(selectors)
            elements = self.page.locator(selector).all()

            if elements:
                # Pick a visible one
                visible_elements = [
                    e for e in elements[:20] if e.is_visible()
                ]  # Check first 20 to avoid perf hit
                if visible_elements:
                    element = random.choice(visible_elements)
                    box = element.bounding_box()
                    if box:
                        x, y = self.get_safe_point(box)
                        self.move_mouse(x, y)
                        self.random_sleep(0.5, 1.5)
        except Exception as e:
            logger.warning(f"Random hover failed: {e}")
