<div align="center">
  <a href="https://github.com/mikumifa/biliTickerBuy" target="_blank">
    <img width="160" src="assets/icon.ico" alt="logo">
  </a>
  <h2 id="koishi">biliTickerBuy</h1>

<p>
  <!-- GitHub Downloads -->
  <a href="https://github.com/mikumifa/biliTickerBuy/releases">
    <img src="https://img.shields.io/github/downloads/mikumifa/biliTickerBuy/total" alt="GitHub all releases">
  </a>
  <!-- GitHub Release Version -->
  <a href="https://github.com/mikumifa/biliTickerBuy/releases">
    <img src="https://img.shields.io/github/v/release/mikumifa/biliTickerBuy" alt="GitHub release (with filter)">
  </a>
  <!-- GitHub Issues -->
  <a href="https://github.com/mikumifa/biliTickerBuy/issues">
    <img src="https://img.shields.io/github/issues/mikumifa/biliTickerBuy" alt="GitHub issues">
  </a>
  <!-- GitHub Stars -->
  <a href="https://github.com/mikumifa/biliTickerBuy/stargazers">
    <img src="https://img.shields.io/github/stars/mikumifa/biliTickerBuy" alt="GitHub Repo stars">
  </a>
</p>
<a href="https://trendshift.io/repositories/11145" target="_blank"><img src="https://trendshift.io/api/badge/repositories/11145" alt="mikumifa%2FbiliTickerBuy | Trendshift" style="width: 250px; height: 55px;" width="250" height="55"/></a>

这是一个开源免费，简单易用的 B 站会员购辅助工具

</div>

## 💻 快速安装

请查看完整的 [安装指南](./docs/installation.md)。

## 固定三人目标与自动回流

本 fork 的个人目标版本使用独立入口 `python -m app_cmd.goal`，支持两个账号共用购票人状态、三人优先、分批补齐、回流等待和重启恢复。先运行离线演示，再按[本机使用指南](./docs/personal-goal.md)配置和进行只读预检。该入口的自动订单核验仍需结合当前账号回包验证，详见[接口证据与边界](./docs/goal-api-evidence.md)。

已有两份账号导出时可用 `import-exports` 自动填写 UID 和购票人映射。准备交由本机 Codex 连续执行时，使用 [Mac 本机执行委托](./docs/mac-commissioning.md)；本轮对购票机会和实际可运行性的审查见[准备情况复核](./docs/goal-readiness-review.md)。

## 👀 使用说明书

前往飞书： https://n1x87b5cqay.feishu.cn/wiki/Eg4xwt3Dbiah02k1WqOcVk2YnMd

## ❗ 项目问题

程序使用问题： [点此链接前往 discussions](https://github.com/mikumifa/biliTickerBuy/discussions)

反馈程序 BUG 或者提新功能建议： [点此链接向项目提出反馈 BUG](https://github.com/mikumifa/biliTickerBuy/issues/new/choose)

## Related Work

Skill版本：https://github.com/mikumifa/biliTickerSkill

分布式版本：https://github.com/mikumifa/biliTickerStorm

## 📩 免责声明
本项目遵循 PolyForm Noncommercial License 1.0.0 许可协议，仅供个人学习与研究使用。未经作者书面授权，不得用于任何商业用途、商业服务、代抢服务或其他营利行为。严禁将本项目用于违法行为或违反相关平台规则的用途。由此产生的一切后果均由使用者自行承担，与作者无关。
若您 fork 或使用本项目，请务必遵守相关法律法规与目标平台规则。
## 🛡️ 平台尊重声明
本项目在设计时严格遵循「非侵入式」原则，避免对目标服务器（如 Bilibili）造成任何干扰。
如本项目中存在侵犯 Bilibili 公司合法权益的内容，请通过邮箱 [1055069518@qq.com](mailto:1055069518@qq.com) 与我联系，我将第一时间下架相关内容并删除本仓库。对此造成的不便，我深表歉意，感谢您的理解与包容。

## 🤩 项目贡献者

<a href="https://github.com/mikumifa/biliTickerBuy/graphs/contributors">
  <img src="https://contrib.rocks/image?repo=mikumifa/biliTickerBuy&preview=true&max=&columns=" />
</a>
<br /><br />

## ⭐️ Star History

[![Star History Chart](https://api.star-history.com/svg?repos=mikumifa/biliTickerBuy&type=Date)](https://www.star-history.com/#mikumifa/biliTickerBuy&Date)
