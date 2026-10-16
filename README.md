# watt-accel

Watt Toolkit（原 Steam++）**网络加速模块**的独立 CLI 提取版。

把 Watt Toolkit 的"网络加速"核心能力单独提取为命令行工具，**零第三方依赖**
——仅使用 Python 3.8+ 标准库，单文件即插即用。

```
watt_accel.py         主程序（约 1000 行，纯标准库）
test_watt_accel.py    单元测试（unittest，纯标准库）
LICENSE              GPL-3.0（本工具为 SteamTools 的衍生作品）
```

## 快速开始

```bash
# 1) 查看可加速的服务列表（实时拉取官方订阅）
python3 watt_accel.py list

# 2) 加速"Steam 社区"（解析域名并写入 hosts，使用指定 DNS）
python3 watt_accel.py apply "Steam 社区" --dns 223.5.5.5

# 3) 查看当前加速状态
python3 watt_accel.py status

# 4) 还原 hosts（删除全部加速记录）
python3 watt_accel.py restore
```

> Windows 请以**管理员**运行；Linux/macOS 请用 `sudo`（写入 /etc/hosts 需要权限）。

## 与原模块的对应关系

| 本 CLI | SteamTools 原模块 | 说明 |
|---|---|---|
| `list` | `Accelerate.All()` | 拉取加速服务订阅（POST `https://api.steampp.net/accelerator/projectgroups`） |
| `apply` | `ProxyMode.Hosts` + `HostsFileServiceImpl.UpdateHosts` | 把选中服务的域名写入 hosts 标记区 |
| `restore` | `RemoveHostsByTag` | 删除标记区、还原备份区原始行 |
| `status` | `ContainsHostsByTag` + `ReadHostsAllLines` | 检测标记区并列出当前加速域名 |
| `--dns / --doh` | 反向代理的 `ProxyDNS` / DoH 加速解析 | 用指定 DNS 解析域名（绕过污染的本地 DNS） |
| `serve` | 本地 443 反向代理（Hosts 模式） | SNI 透传代理：加速域名走快速 DNS 解析 |
| `flushdns` | 平台 DNS 刷新 | `ipconfig /flushdns` / `resolvectl flush-caches` 等 |

## 命令详解

### list — 服务列表

```bash
python3 watt_accel.py list                     # 默认使用缓存（6 小时内）
python3 watt_accel.py list --refresh           # 强制拉取最新
python3 watt_accel.py list --filter Steam      # 关键字过滤
python3 watt_accel.py list --offline           # 仅用本地缓存
```

### apply — 应用加速

```bash
python3 watt_accel.py apply "Steam 社区"                     # 按服务名（子串匹配）
python3 watt_accel.py apply "Steam 图片" "Steam 商店"         # 多个服务
python3 watt_accel.py apply --group "Steam"                   # 整组加速
python3 watt_accel.py apply --all                              # 全部服务
python3 watt_accel.py apply --all --dns 223.5.5.5              # 指定 DNS
python3 watt_accel.py apply --all --doh https://dns.alidns.com/resolve  # DoH
python3 watt_accel.py apply --all --dry-run                    # 预览不落盘
python3 watt_accel.py apply --all --hosts-file /tmp/hosts      # 指定 hosts 路径
```

行为说明：

- 解析选中服务的全部 `ListenDomainNames` 域名，把 `域名 → IP` 写入 hosts 的
  `# Steam++ Start` / `# Steam++ End` 标记区；
- hosts 中原本就存在的同名行会被移入
  `# Steam++ Backup Start` / `# Steam++ Backup End` 备份区，还原时原样恢复；
- **apply 是"精确替换"语义**：本次未选中的旧加速条目会被移除（与 SteamTools
  客户端"停止→重新勾选→启动"的最终效果一致，但不需要先 stop 再 start）；
- 域名解析失败会自动跳过并给出警告，不中断其余条目。

### restore — 还原 hosts

```bash
python3 watt_accel.py restore
python3 watt_accel.py restore --dry-run    # 预览
```

删除标记区与备份区，把备份的原始行插回原位置，hosts 恢复加速前状态。

### serve — 本地加速代理（可选）

```bash
sudo python3 watt_accel.py serve --all --dns 223.5.5.5
python3 watt_accel.py serve --group "Steam" --port 8443 --host 127.0.0.1
```

SNI 透传代理：客户端把 HTTPS 流量指向本代理（如 Steam 下载设置/系统代理），
代理读取 TLS 握手里的 SNI，命中加速项目的域名走指定 DNS 解析后转发，未命中的
域名走系统 DNS 透传。**不做中间人解密**，因此无需安装证书，也不支持脚本注入。

配合 hosts 模式使用：`apply --target 127.0.0.1` 可把加速域名指向本地代理。

## DNS 解析方式（对应"加速"的本质）

| 参数 | 解析方式 | 示例 |
|---|---|---|
| （默认） | 系统 DNS | — |
| `--dns IP` | 直连指定 DNS 服务器（内置 UDP+TCP DNS 客户端） | `223.5.5.5`、`1.1.1.1` |
| `--doh URL` | DNS over HTTPS（JSON 接口） | `https://dns.alidns.com/resolve` |

`--dns` 与 `--doh` 均内置实现、零依赖；`--ipv6` 可优先解析 AAAA 记录。

## 测试

```bash
python3 test_watt_accel.py
```

覆盖：hosts 写入/还原往返一致性、二次 apply 备份保持、标记区外同名行备份、
V1 旧格式清理、孤立标记清理、注释/无效行保留、重复标记报错、UTF-8 BOM 保留、
DNS 报文编解码、SNI 解析与域名匹配、订阅响应解析、CLI 参数行为。

## 与 SteamTools 的两处有意差异

1. **apply 替换语义**：原 `UpdateHosts` 会保留标记区内所有旧条目（由客户端
   的停止流程负责清除）；CLI 的 apply 直接替换为当前选择，避免残留。
2. **备份永不退化**：原实现二次 apply 后部分备份会退化为不可还原的裸行；
   本实现始终以 `#行号 原始行` 形式保留备份，多次 apply 后 restore 依然精确。

## 限制（未包含的部分）

- 不做 HTTPS 中间人解密（原模块的脚本注入、响应改写、GOG 代理等需要证书体系）；
- 不支持 `ServerAccelerate`（服务端加速，需要登录与令牌）；
- `Redirect` 类型项目的 `ForwardDomainNames` 依赖 SteamTools 服务端，本 CLI
  一律按原始域名解析；
- `apply` 写入的是解析得到的 IP；如 IP 失效可重新 apply 刷新。

## 许可证

本工具是 [BeyondDimension/SteamTools](https://github.com/BeyondDimension/SteamTools)
（GPL-3.0）网络加速模块的衍生作品，按 **GPL-3.0** 协议分发，随附 LICENSE 全文。
