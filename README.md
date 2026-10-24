# DS余额查询与控制

一个 AstrBot 插件：查询 **DeepSeek** 账户余额，并在余额偏低时按配置发送收款码。

## 功能

- **余额查询**：调用 DeepSeek 官方 `GET /user/balance`，返回可用总额、充值余额、赠送余额
- **LLM 工具**：`get_api_balance` 让模型能自己回答「你还有多少钱」
- **收款码管理**：面板直接上传，或管理员在聊天里发图；发送时不带具体金额
- **低余额联动**：低于阈值时有两种策略
  - `ask_model`：只把「可以发收款码」提示给模型，由它决定
  - `auto_send`：插件自己在后台判定并发送，按冷却时间限制频率
- **可见性控制**：余额可设为所有人可见，或仅管理员可见（其他人只会得到模糊提示）
- **权限**：修改收款码仅限管理员（AstrBot 的 `admins_id`）

## 安装

在 AstrBot 插件市场搜索 `DS余额查询与控制`，或：

```bash
git clone https://github.com/Mao19927/astrbot_plugin_dsbalance \
  AstrBot/data/plugins/astrbot_plugin_dsbalance
```

重启 AstrBot 后在插件页启用。

## 命令

| 命令 | 说明 | 权限 |
|---|---|---|
| `/余额` | 查询账户余额 | 受 `balance_visibility` 控制 |
| `/收款码` | 查看当前收款码 | 所有人 |
| `/收款码` + 图片 | 上传/替换收款码 | 仅管理员 |
| `/收款码 清除` | 删除收款码 | 仅管理员 |

## 配置说明

| 配置项 | 默认 | 说明 |
|---|---|---|
| `api_key` | 空 | DeepSeek API Key。留空则自动读取 AstrBot 中已配置的 DeepSeek 服务商 |
| `payment_qr` | 空 | 收款码图片，支持面板上传多张（用第一张） |
| `low_balance_threshold` | 5.0 | 低于该值视为余额不足，填 0 关闭提醒 |
| `balance_visibility` | `everyone` | `everyone` / `admin_only` |
| `pay_mode` | `ask_model` | `ask_model` / `auto_send` |
| `auto_send_cooldown_hours` | 6.0 | `auto_send` 两次发送的最小间隔 |
| `auto_check_interval_minutes` | 30 | 后台检查间隔 |
| `auto_send_umo` | 空 | 自动发送目标会话，留空发最近一次私聊 |
| `qr_caption` | 大白饭快吃光了，求你给我打点米吧QwQ | 发码配文（不含金额） |
| `vague_low_text` | 见默认值 | 无权限用户的低余额提示 |
| `hint_text` | 见默认值 | 有权限时的低余额提示 |

## 关于 API Key

插件按以下顺序获取 DeepSeek Key：

1. 插件配置里的 `api_key`
2. AstrBot 主配置 `provider_sources` 中 `provider=deepseek` 的第一条 Key

第 2 条意味着：**如果你已经给 AstrBot 配了 DeepSeek，就不用再填一次**；不放心的话填上 `api_key`，插件就不会去读主配置。

## 数据与隐私

- 只请求 DeepSeek 官方接口 `api.deepseek.com/user/balance`，不向任何第三方发送数据
- 收款码保存在 `data/plugin_data/astrbot_plugin_dsbalance/`，不上传
- 自动发送记录（时间戳、最近会话）保存在同目录 `state.json`，可随时删除

## 限制

- 仅支持 DeepSeek 官方 API 的余额接口；其他服务商（OpenAI、百炼等）没有对应的余额 API
- 需要 AstrBot `>= 4.17.0`

## License

MIT
