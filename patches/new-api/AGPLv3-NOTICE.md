# AGPLv3 通知与署名

本补丁修改的是 **QuantumNous/new-api**，上游作者及项目署名保持不变。

- 上游项目：https://github.com/QuantumNous/new-api
- 固定发布版本：`v1.0.0-rc.49`
- 固定 commit：`48a6c87f9e7757fdc58e5be9bcb4f8281ad0dfd0`
- 上游许可：GNU Affero General Public License v3.0（AGPLv3）
- 许可全文：https://github.com/QuantumNous/new-api/blob/48a6c87f9e7757fdc58e5be9bcb4f8281ad0dfd0/LICENSE

本目录的补丁及 `new-api/Dockerfile` 是对上述 AGPLv3 项目的修改。修改后的程序仍按 AGPLv3 提供，不删除或替换上游版权、许可及署名。构建流程保留上游 `LICENSE`、`NOTICE` 和 `THIRD-PARTY-LICENSES.md`，并安装到镜像的 `/licenses/`。

如果通过网络向用户提供修改后程序的服务，AGPLv3 第 13 条要求向这些用户提供对应的完整源码。对应源码由固定上游源码与本目录补丁共同组成；请同时保留并提供上游获取地址、commit、补丁和构建文件。
