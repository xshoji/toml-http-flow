import unittest
import os
import uuid

from httpflow.template import TemplateError, render, render_mapping


class TestRender(unittest.TestCase):
    def setUp(self):
        self.store = {
            "vars": {"env": "production", "user": "alice", "token": "abc123"},
        }

    def test_simple_var(self):
        self.assertEqual(render("env=${var.env}", self.store), "env=production")

    def test_top_level_var_alias(self):
        self.assertEqual(render("env=${env}", self.store), "env=production")

    def test_captured_var(self):
        self.assertEqual(
            render("Bearer ${token}", self.store),
            "Bearer abc123",
        )

    def test_multiple_refs(self):
        out = render("${var.user}@${var.env}", self.store)
        self.assertEqual(out, "alice@production")

    def test_dollar_escape(self):
        self.assertEqual(render('{"price":"$$100"}', self.store), '{"price":"$100"}')

    def test_missing_var(self):
        with self.assertRaises(TemplateError):
            render("${var.unknown}", self.store)

    def test_missing_nested_path(self):
        with self.assertRaises(TemplateError):
            render("${steps.nonexistent.key}", self.store)

    def test_render_mapping(self):
        m = {"Authorization": "Bearer ${token}", "X-Env": "${var.env}"}
        self.assertEqual(
            render_mapping(m, self.store),
            {"Authorization": "Bearer abc123", "X-Env": "production"},
        )

    def test_no_placeholders(self):
        self.assertEqual(render("plain text", self.store), "plain text")

    def test_hyphen_in_captured_var_name(self):
        store = {
            "vars": {"argsAaa2": "hello"},
        }
        self.assertEqual(
            render("v=${argsAaa2}", store),
            "v=hello",
        )

    def test_hyphen_in_var_name(self):
        store = {"vars": {"my-key": "ok"}, "steps": {}}
        self.assertEqual(render("${var.my-key}", store), "ok")

    def test_random_uuid(self):
        out = render("id=${random.UUID}", self.store)
        value = out.removeprefix("id=")
        self.assertEqual(str(uuid.UUID(value)), value)

    def test_random_uuid_hex(self):
        out = render("id=${random.UUID_HEX}", self.store)
        value = out.removeprefix("id=")
        self.assertEqual(len(value), 32)
        self.assertEqual(uuid.UUID(hex=value).hex, value)

    def test_env_var(self):
        old = os.environ.get("HTTPFLOW_TEST_USER")
        os.environ["HTTPFLOW_TEST_USER"] = "bob"
        try:
            self.assertEqual(render("user=${env.HTTPFLOW_TEST_USER}", self.store), "user=bob")
        finally:
            if old is None:
                os.environ.pop("HTTPFLOW_TEST_USER", None)
            else:
                os.environ["HTTPFLOW_TEST_USER"] = old

    def test_missing_env_var(self):
        os.environ.pop("HTTPFLOW_TEST_MISSING", None)
        with self.assertRaises(TemplateError):
            render("${env.HTTPFLOW_TEST_MISSING}", self.store)


if __name__ == "__main__":
    unittest.main()
