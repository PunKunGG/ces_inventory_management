"""Optional browser acceptance with an installed Selenium/ChromeDriver.

Run using: python -m unittest discover -s tests -p browser_smoke.py -v
Set CES_TEST_CHROME and CES_TEST_CHROMEDRIVER for non-default binary paths.
Set CES_TEST_SCREENSHOTS to an optional output directory for visual review.
"""

import os
import threading
from pathlib import Path
from urllib.parse import urlparse

from werkzeug.serving import make_server

from ces import create_app
from test_acceptance import InventoryFixture, PASSWORD


class MobileBrowserAcceptance(InventoryFixture):
    def setUp(self):
        try:
            from selenium import webdriver
            from selenium.webdriver.chrome.service import Service
        except ImportError:
            self.skipTest("Optional browser check requires Selenium")

        chrome = Path(os.environ.get(
            "CES_TEST_CHROME", "C:/ChromeForTesting/chrome-win64/chrome.exe"
        ))
        driver = Path(os.environ.get(
            "CES_TEST_CHROMEDRIVER",
            "C:/ChromeForTesting/chromedriver-win64/chromedriver.exe",
        ))
        if not chrome.is_file() or not driver.is_file():
            self.skipTest("Set CES_TEST_CHROME and CES_TEST_CHROMEDRIVER to installed binaries")
        super().setUp()
        self.app = create_app(self.config)
        server = make_server("127.0.0.1", 0, self.app, threaded=True)
        self.server_url = f"http://127.0.0.1:{server.server_port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 5)
        self.addCleanup(server.shutdown)

        options = webdriver.ChromeOptions()
        options.binary_location = str(chrome)
        options.add_argument("--headless=new")
        options.add_argument("--disable-background-networking")
        options.add_argument("--window-size=1280,1024")
        options.add_experimental_option("mobileEmulation", {
            "deviceMetrics": {"width": 390, "height": 844, "pixelRatio": 3, "touch": True, "mobile": True},
            "clientHints": {"platform": "Android", "mobile": True},
        })
        self.browser = webdriver.Chrome(service=Service(str(driver)), options=options)
        self.addCleanup(self.browser.quit)

    def open(self, route):
        self.browser.get(self.server_url + route)

    def path_is(self, route):
        from selenium.webdriver.support.ui import WebDriverWait

        WebDriverWait(self.browser, 5).until(
            lambda browser: urlparse(browser.current_url).path == route
        )
        self.assertEqual(urlparse(self.browser.current_url).path, route)

    def element(self, selector):
        from selenium.webdriver.common.by import By

        return self.browser.find_element(By.CSS_SELECTOR, selector)

    def fill(self, name, value):
        element = self.element(f'[name="{name}"]')
        element.clear()
        element.send_keys(str(value))

    def select(self, name, value):
        from selenium.webdriver.support.ui import Select

        Select(self.element(f'[name="{name}"]')).select_by_value(str(value))

    def submit_operation(self):
        from selenium.webdriver.common.keys import Keys

        button = self.element('[data-stock-form] button[type="submit"]')
        invalid_fields = self.browser.execute_script(
            "return Array.from(arguments[0].form.elements).filter(element => "
            "element.willValidate && !element.validity.valid).map(element => element.name);",
            button,
        )
        self.assertEqual(invalid_fields, [])
        self.browser.execute_script("arguments[0].focus()", button)
        self.assertEqual(self.browser.switch_to.active_element, button)
        button.send_keys(Keys.SPACE)

    def screenshot(self, name):
        destination = os.environ.get("CES_TEST_SCREENSHOTS")
        if destination:
            directory = Path(destination)
            directory.mkdir(parents=True, exist_ok=True)
            self.browser.save_screenshot(str(directory / f"{name}.png"))

    def assert_no_page_overflow(self):
        viewport, page = self.browser.execute_script(
            "return [window.innerWidth, document.documentElement.scrollWidth];"
        )
        self.assertEqual(viewport, 390, "Browser must use the narrow test viewport")
        self.assertLessEqual(page, viewport)

    def test_mobile_entrance_and_inventory_operations(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys

        tool_id = self.register_tool()
        self.open("/")
        self.path_is("/login")
        self.assert_no_page_overflow()
        self.screenshot("mobile-login")
        username = self.element("#username")
        username.send_keys("tech", Keys.TAB)
        self.assertEqual(self.browser.switch_to.active_element.get_attribute("id"), "password")
        password = self.element("#password")
        password.send_keys(PASSWORD, Keys.TAB)
        submit = self.element('.login-form button[type="submit"]')
        self.assertEqual(self.browser.switch_to.active_element, submit)
        submit.send_keys(Keys.SPACE)
        self.path_is("/dashboard")
        self.assert_no_page_overflow()
        self.screenshot("mobile-dashboard")
        self.assertFalse(self.browser.find_elements(By.CSS_SELECTOR, 'a[href="/history"]'))
        menu = self.element(".menu-toggle")
        self.assertEqual(menu.get_attribute("aria-expanded"), "false")
        menu.click()
        self.assertEqual(menu.get_attribute("aria-expanded"), "true")
        self.element("#site-navigation a").send_keys(Keys.ESCAPE)
        self.assertEqual(menu.get_attribute("aria-expanded"), "false")
        self.assertEqual(self.browser.switch_to.active_element, menu)
        self.open("/")
        self.path_is("/dashboard")

        self.open("/operations/stock_in")
        self.assert_no_page_overflow()
        self.select("item_id", 1)
        self.fill("quantity", 2)
        self.assertEqual(self.element("[data-stock-before]").text, "0")
        self.assertEqual(self.element("[data-stock-after]").text, "2")
        self.screenshot("mobile-receive")
        self.submit_operation()
        self.path_is("/items/1")
        self.assertEqual(self.quantity(), 2)

        self.open("/operations/stock_out")
        self.select("item_id", 1)
        self.fill("quantity", 3)
        self.select("requester_id", 4)
        self.fill("purpose", "ทดสอบหน้าจอมือถือ")
        self.submit_operation()
        self.path_is("/operations/stock_out")
        self.assertEqual(self.element('[name="quantity"]').get_property("value"), "3")
        self.assertEqual(self.element('[name="purpose"]').get_property("value"), "ทดสอบหน้าจอมือถือ")
        self.assertEqual(self.quantity(), 2)
        self.fill("quantity", 1)
        self.submit_operation()
        self.path_is("/items/1")
        self.assertEqual(self.quantity(), 1)

        self.open("/operations/loan")
        self.select("tool_unit_id", tool_id)
        self.select("requester_id", 4)
        self.select("responsible_id", 3)
        due = self.element('[name="due_at"]')
        self.browser.execute_script(
            "arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('change'));",
            due,
            self.due_date,
        )
        self.submit_operation()
        self.path_is("/items/4")
        self.assertEqual(self.tool_status(tool_id), "loaned")

        self.open("/operations/return")
        self.select("tool_unit_id", tool_id)
        loan_id = self.db.execute("SELECT id FROM item_loans").fetchone()[0]
        self.assertEqual(self.element('[name="loan_id"]').get_property("value"), str(loan_id))
        self.select("condition", "damaged")
        self.submit_operation()
        self.path_is("/items/4")
        self.assertEqual(self.tool_status(tool_id), "damaged")

        self.element(".menu-toggle").click()
        self.element('form[action="/logout"] button').click()
        self.path_is("/login")
        self.open("/")
        self.path_is("/login")
