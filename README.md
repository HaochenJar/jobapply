# jobapply

用 Claude Code 半自动投递求职申请的插件，面向 F-1/OPT 应届生。
A Claude Code plugin for semi-automated job applications, built for F-1/OPT new grads.

**discover** (Greenhouse / Lever / Ashby / Workday / LinkedIn) → **score** (确定性打分，不用 LLM)
→ **tailor** (简历+求职信，每个事实都可追溯到你的 master.md) → **apply** (Chrome 填表并逐项校验)
→ **inbox** (从 Gmail 归类 ATS 邮件)。

## 安装 Install

在 Claude Code 里 / inside Claude Code:

```
/plugin marketplace add HaochenJar/jobapply
/plugin install jobapply@jobapply
```

或者在终端 / or from a terminal:

```bash
claude plugin marketplace add HaochenJar/jobapply && claude plugin install jobapply@jobapply
```

然后重启 Claude Code，说一句 **"set up jobapply"**，Claude 会带你走完初始化：
Then restart Claude Code and say **"set up jobapply"**. Claude walks you through it:

1. 把引擎装到 `~/jobapply/`，在 `~/.config/jobapply/profile.json` 生成空白 profile
2. 你**亲手**填 profile（身份、薪资、OPT 开始日期等，见 `INTAKE.md`）
3. 把你的简历变成 `~/jobapply/docs/master.md`，它是唯一事实来源
4. 按你的简历和目标改模板（`docs.py` / `score.py` 里搜 `PERSONALIZE`）
5. 所有 selftest 通过后才开始真正投递

**依赖 Requirements:** macOS, Python 3.9+, Google Chrome, `pandoc` (`brew install pandoc`),
the Claude in Chrome extension (for filling forms), and a Gmail
connector (for the inbox step).

## 每日流程 Daily loop

```bash
cd ~/jobapply
./run.sh discover   # 抓新职位
./run.sh score      # 打分
./run.sh tailor     # 生成简历 + 求职信
./run.sh apply      # Chrome 填表
./run.sh inbox      # 归类邮件（每天 2-3 次）
./run.sh status     # 只需要关心 needs_human 的数量
```

## 安全闸门 Human gates

- `auto_submit` 默认关闭：每份申请都停在已校验的草稿，等你确认后才提交。
- Tier-1 公司（`companies.json`）永远只出草稿，一家公司通常只有一次机会。
- CAPTCHA、陌生问题、字段校验不一致 → 进 `needs_human`，绝不猜。
- 投递前就要 SSN / 生日 / 护照 / 银行信息 → 直接中止，并标记为疑似诈骗。
- 不创建账号、不输入密码；邮件只写草稿，从不自动发送。
- 每家公司每周最多投 2 份（Greenhouse 的 spam 标记按雇主计，而且永久）。

LinkedIn 和 Indeed 的用户协议禁止自动化申请，用不用这两个渠道由你自己决定。
