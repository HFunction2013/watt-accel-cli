#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
watt_accel 的单元测试（仅标准库 unittest）。

运行：
    python3 test_watt_accel.py

覆盖：
  - hosts 引擎：首次 apply / restore 精确还原 / 二次 apply 备份保持 /
    标记区外同名行备份 / V1 格式清理 / 孤立标记清理 / 注释与无效行保留 /
    重复标记报错 / 空行修剪 / 编码(UTF-8 BOM)
  - DNS：自建 DNS 客户端报文编解码、CNAME 压缩指针解析（本地验证）
  - SNI：ClientHello 解析与域名匹配
  - 订阅响应解析（构造数据）
"""

import io
import json
import os
import struct
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import watt_accel as wa


def make_hosts_file(content: str):
    """写一个临时 hosts 文件，返回路径。"""
    fd, path = tempfile.mkstemp(prefix="hosts_test_", suffix=".hosts")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


class TestHostsEngine(unittest.TestCase):
    def setUp(self):
        self.path = None

    def tearDown(self):
        if self.path and os.path.exists(self.path):
            os.unlink(self.path)

    def _read(self):
        with open(self.path, "r", encoding="utf-8") as f:
            return f.read()

    def test_apply_and_restore_roundtrip(self):
        original = (
            "127.0.0.1 localhost\n"
            "# 用户注释\n"
            "192.168.1.1 router\n"
            "10.0.0.1 old-domain.example\n"
        )
        self.path = make_hosts_file(original)

        # apply
        wa.handle_hosts(True, {"steamcommunity.com": "1.2.3.4",
                               "www.steamcommunity.com": "1.2.3.4"},
                        self.path)
        content = self._read()
        self.assertIn("# Steam++ Start", content)
        self.assertIn("1.2.3.4 steamcommunity.com", content)
        self.assertIn("1.2.3.4 www.steamcommunity.com", content)
        self.assertIn("# Steam++ End", content)
        # 原行仍在
        self.assertIn("127.0.0.1 localhost", content)
        self.assertIn("# 用户注释", content)
        self.assertIn("192.168.1.1 router", content)

        # restore
        wa.handle_hosts(False, None, self.path)
        self.assertEqual(self._read(), original)

    def test_apply_replaces_existing_line_outside_marker(self):
        # 标记区外已存在同域名行 → 原行移入备份区
        original = (
            "127.0.0.1 localhost\n"
            "9.9.9.9 github.com\n"
        )
        self.path = make_hosts_file(original)
        wa.handle_hosts(True, {"github.com": "1.1.1.1"}, self.path)
        content = self._read()
        self.assertIn("1.1.1.1 github.com", content)
        # 原行不再作为独立 hosts 行存在（其内容进入备份区 "#2 ..." 行）
        self.assertNotIn("\n9.9.9.9 github.com\n", content)
        self.assertIn("# Steam++ Backup Start", content)
        # 还原后原行恢复
        wa.handle_hosts(False, None, self.path)
        self.assertEqual(self._read(), original)

    def test_apply_twice_then_restore_exact(self):
        original = (
            "127.0.0.1 localhost\n"
            "9.9.9.9 github.com\n"
            "8.8.8.8 steamcommunity.com\n"
        )
        self.path = make_hosts_file(original)
        # 第一次 apply：github + steamcommunity
        wa.handle_hosts(True, {"github.com": "1.1.1.1",
                               "steamcommunity.com": "2.2.2.2"}, self.path)
        # 第二次 apply：只更新 github（steamcommunity 从加速列表移除）
        wa.handle_hosts(True, {"github.com": "3.3.3.3"}, self.path)
        content = self._read()
        self.assertIn("3.3.3.3 github.com", content)
        self.assertNotIn("2.2.2.2", content)
        # 被移除的 steamcommunity 原行应已还原
        self.assertIn("8.8.8.8 steamcommunity.com", content)
        # restore 后应完整还原
        wa.handle_hosts(False, None, self.path)
        self.assertEqual(self._read(), original)

    def test_v1_format_removed(self):
        self.path = make_hosts_file(
            "127.0.0.1 localhost\n"
            "1.1.1.1 steamcommunity.com #Steam++\n"
        )
        wa.handle_hosts(True, {"steamcommunity.com": "2.2.2.2"}, self.path)
        content = self._read()
        self.assertNotIn("#Steam++", content)
        self.assertIn("2.2.2.2 steamcommunity.com", content)
        wa.handle_hosts(False, None, self.path)
        self.assertIn("127.0.0.1 localhost", self._read())
        # V1 行不参与还原（原实现语义）
        self.assertNotIn("1.1.1.1 steamcommunity.com", self._read())

    def test_orphan_end_marker_removed(self):
        self.path = make_hosts_file(
            "127.0.0.1 localhost\n"
            "# Steam++ End\n"
        )
        wa.handle_hosts(False, None, self.path)
        self.assertNotIn("# Steam++ End", self._read())
        self.assertIn("127.0.0.1 localhost", self._read())

    def test_invalid_and_comment_lines_preserved(self):
        self.path = make_hosts_file(
            "127.0.0.1 localhost\n"
            "# 注释\n"
            "  \n"
            "badline withoutcomment\n"
        )
        wa.handle_hosts(True, {"a.com": "1.1.1.1"}, self.path)
        content = self._read()
        for keep in ("# 注释", "badline withoutcomment"):
            self.assertIn(keep, content)
        self.assertIn("1.1.1.1 a.com", content)

    def test_duplicate_mark_raises(self):
        self.path = make_hosts_file(
            "# Steam++ Start\n1.1.1.1 a.com\n# Steam++ End\n"
            "# Steam++ Start\n2.2.2.2 b.com\n# Steam++ End\n"
        )
        with self.assertRaises(wa.DuplicateMarkError):
            wa.handle_hosts(True, {"c.com": "3.3.3.3"}, self.path)

    def test_marker_block_updates_in_place(self):
        self.path = make_hosts_file(
            "127.0.0.1 localhost\n"
            "# Steam++ Start\n"
            "1.1.1.1 a.com\n"
            "2.2.2.2 b.com\n"
            "# Steam++ End\n"
        )
        wa.handle_hosts(True, {"a.com": "9.9.9.9"}, self.path)
        content = self._read()
        self.assertIn("9.9.9.9 a.com", content)
        self.assertNotIn("1.1.1.1 a.com", content)
        self.assertNotIn("2.2.2.2 b.com", content)  # 未选中的被移除
        self.assertIn("127.0.0.1 localhost", content)

    def test_utf8_bom_preserved(self):
        fd, path = tempfile.mkstemp(prefix="hosts_bom_", suffix=".hosts")
        with os.fdopen(fd, "wb") as f:
            f.write(b"\xef\xbb\xbf127.0.0.1 localhost\n")
        self.path = path
        wa.handle_hosts(True, {"a.com": "1.1.1.1"}, self.path)
        with open(self.path, "rb") as f:
            raw = f.read()
        self.assertTrue(raw.startswith(b"\xef\xbb\xbf"))
        self.assertIn(b"1.1.1.1 a.com", raw)

    def test_dry_run_no_write(self):
        original = "127.0.0.1 localhost\n"
        self.path = make_hosts_file(original)
        result = wa.handle_hosts(True, {"a.com": "1.1.1.1"}, self.path, dry_run=True)
        self.assertTrue(result["changed"])
        self.assertEqual(self._read(), original)
        # dry-run 的还原也不写
        wa.handle_hosts(False, None, self.path, dry_run=True)
        self.assertEqual(self._read(), original)


class TestSubscriptionParse(unittest.TestCase):
    def test_emoji_wrapper(self):
        rsp = {"🦓": [{"Name": "Steam 服务", "Items": []}], "🦄": 200, "🐴": None}
        data = wa.parse_subscribe_response(rsp)
        self.assertEqual(data[0]["Name"], "Steam 服务")

    def test_plain_list(self):
        self.assertEqual(wa.parse_subscribe_response([1, 2]), [1, 2])

    def test_empty_ok(self):
        self.assertEqual(wa.parse_subscribe_response({"🦄": 200}), [])

    def test_bad_format_raises(self):
        with self.assertRaises(RuntimeError):
            wa.parse_subscribe_response({"🦄": 500})


class TestSelectAndDomains(unittest.TestCase):
    def setUp(self):
        self.groups = [
            {"Name": "Steam 服务", "Items": [
                {"Name": "Steam 社区", "ListenDomainNames": "steamcommunity.com;www.steamcommunity.com",
                 "Items": [
                     {"Name": "Steam 社区(解锁访问限制)",
                      "ListenDomainNames": "steamcommunity.com"},
                 ]},
                {"Name": "Steam 商店", "ListenDomainNames": "store.steampowered.com"},
            ]},
            {"Name": "Github", "Items": [
                {"Name": "Github UserContent",
                 "ListenDomainNames": "raw.githubusercontent.com;githubusercontent.com"},
            ]},
        ]

    def test_select_by_name(self):
        selected = wa.select_items(self.groups, names=["Steam 社区"])
        self.assertEqual(len(selected), 1)
        self.assertEqual(collect_len(selected), ["steamcommunity.com", "www.steamcommunity.com"])

    def test_select_child_item(self):
        selected = wa.select_items(self.groups, names=["解锁访问限制"])
        self.assertEqual(len(selected), 1)
        self.assertEqual(collect_len(selected), ["steamcommunity.com"])

    def test_select_by_group(self):
        selected = wa.select_items(self.groups, group_names=["Github"])
        self.assertEqual(collect_len(selected), ["raw.githubusercontent.com", "githubusercontent.com"])

    def test_select_all(self):
        selected = wa.select_items(self.groups, all_flag=True)
        self.assertEqual(len(selected), 3)

    def test_case_insensitive(self):
        selected = wa.select_items(self.groups, names=["steam 商店"])
        self.assertEqual(collect_len(selected), ["store.steampowered.com"])


def collect_len(selected):
    return wa.collect_listen_domains(selected)


class TestDnsCodec(unittest.TestCase):
    """DNS 报文编解码的本地验证（不依赖网络）。"""

    def test_encode_name(self):
        self.assertEqual(wa._encode_name("steamcommunity.com"),
                         b"\x0esteamcommunity\x03com\x00")

    def test_decode_name_plain(self):
        data = b"\x03www\x07example\x03com\x00" + b"\x00\x01"
        name, end = wa._decode_name(data, 0)
        self.assertEqual(name, "www.example.com")
        self.assertEqual(end, 17)

    def test_decode_name_pointer(self):
        # 名称指向偏移 12 处
        data = bytearray(12)
        data.extend(b"\x03abc\x00")           # 偏移 12 处: abc（12..16）
        data.extend(b"\xc0\x0c")               # 偏移 17 处: 指针 → 12
        name, end = wa._decode_name(bytes(data), 17)
        self.assertEqual(name, "abc")
        self.assertEqual(end, 19)

    def test_parse_response_a_record(self):
        # 构造应答：1 问题，1 答案 A 1.2.3.4
        header = struct.pack(">HHHHHH", 0x1234, 0x8180, 1, 1, 0, 0)
        q = wa._encode_name("a.com") + struct.pack(">HH", 1, 1)
        ans = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + bytes([1, 2, 3, 4])
        answers = wa.parse_dns_response(header + q + ans)
        self.assertIn(("A", "1.2.3.4"), answers)

    def test_parse_response_cname(self):
        header = struct.pack(">HHHHHH", 0x1234, 0x8180, 1, 2, 0, 0)
        q = wa._encode_name("a.com") + struct.pack(">HH", 1, 1)
        cname = b"\xc0\x0c" + struct.pack(">HHIH", 5, 1, 60, 7) + wa._encode_name("b.com")
        a = b"\xc0\x0c" + struct.pack(">HHIH", 1, 1, 60, 4) + bytes([5, 6, 7, 8])
        answers = wa.parse_dns_response(header + q + cname + a)
        self.assertIn(("CNAME", "b.com"), answers)
        self.assertIn(("A", "5.6.7.8"), answers)


class TestSni(unittest.TestCase):
    def _make_client_hello(self, sni: str) -> bytes:
        """构造带 SNI 的最小 ClientHello。"""
        name = sni.encode()
        sni_ext = struct.pack(">H", 0)  # type=server_name
        sni_body = b"\x00" + struct.pack(">H", len(name)) + name  # name_type + len + name
        sni_list = struct.pack(">H", len(sni_body)) + sni_body
        sni_ext += struct.pack(">H", len(sni_list)) + sni_list
        # extensions
        exts = sni_ext
        hello_body = b"\x03\x03" + bytes(32) + b"\x00" + b"\x00\x02\x13\x01" + b"\x01\x00"
        hello_body += struct.pack(">H", len(exts)) + exts
        hello = b"\x01" + struct.pack(">I", len(hello_body))[1:] + hello_body
        record = b"\x16\x03\x01" + struct.pack(">H", len(hello)) + hello
        return record

    def test_parse_sni(self):
        hello = self._make_client_hello("steamcommunity.com")
        self.assertEqual(wa.parse_client_hello_sni(hello), "steamcommunity.com")

    def test_parse_sni_none(self):
        self.assertIsNone(wa.parse_client_hello_sni(b"\x16\x03\x01\x00\x02\x01\x00"))

    def test_domain_match(self):
        listen = ["steamcommunity.com", "github.com"]
        self.assertTrue(wa.domain_matches("steamcommunity.com", listen))
        self.assertTrue(wa.domain_matches("www.steamcommunity.com", listen))
        self.assertTrue(wa.domain_matches("api.steamcommunity.com", listen))
        self.assertTrue(wa.domain_matches("GITHUB.COM", listen))
        self.assertFalse(wa.domain_matches("steamcommunity.com.evil.com", listen))
        self.assertFalse(wa.domain_matches("example.org", listen))


class TestDnsResolution(unittest.TestCase):
    """依赖公网 DNS 的解析测试（无网络时自动跳过）。"""

    def test_dns_query_real(self):
        try:
            ip = wa.resolve_with_dns_server("www.baidu.com", "223.5.5.5", 1)
        except Exception as e:
            self.skipTest("无网络: %s" % e)
        if not ip:
            self.skipTest("DNS 查询无响应")
        self.assertRegex(ip, r"^\d+\.\d+\.\d+\.\d+$")

    def test_system_resolve(self):
        ip = wa.resolve_system("www.baidu.com", 1)
        if not ip:
            self.skipTest("系统 DNS 无响应")
        self.assertRegex(ip, r"^\d+\.\d+\.\d+\.\d+$")


class TestCli(unittest.TestCase):
    def test_list_offline(self):
        fd, cache = tempfile.mkstemp()
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump([{"Name": "T", "Items": [
                {"Name": "S1", "ListenDomainNames": "a.com;b.com"}]}], f, ensure_ascii=False)
        old = wa.CACHE_FILE
        wa.CACHE_FILE = cache
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = wa.main(["list", "--offline"])
            self.assertEqual(rc, 0)
            self.assertIn("S1", buf.getvalue())
        finally:
            wa.CACHE_FILE = old
            os.unlink(cache)

    def test_apply_no_selection(self):
        fd, cache = tempfile.mkstemp()
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump([], f)
        old = wa.CACHE_FILE
        wa.CACHE_FILE = cache
        try:
            rc = wa.main(["apply", "不存在的服务", "--offline"])
            self.assertEqual(rc, 1)
        finally:
            wa.CACHE_FILE = old
            os.unlink(cache)


if __name__ == "__main__":
    unittest.main(verbosity=2)
