-- crypto-llm-mvp 首次初始化：创建三个应用数据库。
-- 三个应用（new_api / epusdt / adapter）均以 postgres 超级用户连接
-- （DSN 中用户名固定为 postgres），因此这里不另建独立角色。
-- postgres 官方镜像会在首次初始化时自动执行 /docker-entrypoint-initdb.d/ 下的脚本。

CREATE DATABASE new_api;
CREATE DATABASE epusdt;
CREATE DATABASE adapter;
