# AutoList SQLite 备份、升级与恢复

本文适用于当前 `1.03` 的本地 tag 部署。AutoList 的数据库、运行设置、CookieCloud 密文和日志都位于容器的 `/data`；Unraid 默认宿主机路径为 `/mnt/user/appdata/Autolist/data`。这些命令只在管理员明确执行时才会改变容器或数据，本文不会自动连接或修改任何真实环境。

## 重要原则

- 备份整个 `data` 目录，不要只复制 `playlist-autodown.db`。
- 数据库使用 SQLite WAL。应用运行时可能同时存在 `playlist-autodown.db-wal` 和 `playlist-autodown.db-shm`；不要在容器运行时只复制主数据库文件，也不要手工删除 WAL/SHM 文件。
- 最稳妥的文件级备份方式是先停止 AutoList，再打包整个 `data` 目录。停止期间不会访问 TMDB、PT、Emby、Transmission 或 MoviePilot。
- 备份文件包含运行设置和 CookieCloud 密文，应放在仅管理员可读的目录，不要上传到公共仓库、工单或聊天记录。
- healthcheck 只确认本地应用和 SQLite 可用，不代表外部下游服务正常。

## 升级前只读检查

先确认目标、容器和挂载路径，没有确认路径前不要执行恢复或递归权限修改：

```bash
docker inspect --format '{{.Name}} image={{.Config.Image}} status={{.State.Status}} mount={{range .Mounts}}{{.Source}}:{{.Destination}} {{end}}' Autolist
stat -c '%u:%g %a %n' /mnt/user/appdata/Autolist/data
docker inspect --format '{{.State.Health.Status}}' Autolist
```

运行中的数据库可以进行一次只读的完整检查。它可能比 healthcheck 的 `quick_check(1)` 更耗时，但不会写入数据库：

```bash
docker exec Autolist python -c 'import sqlite3; db=sqlite3.connect("/data/playlist-autodown.db", timeout=5); result=db.execute("PRAGMA integrity_check").fetchall(); assert result == [("ok",)], result; assert not db.execute("PRAGMA foreign_key_check").fetchall(); print("SQLite integrity: ok")'
```

如果目录不是 UID `10001` 所有，先完成备份，再停止容器并修正权限。镜像以非 root UID `10001` 运行，错误的宿主机权限会导致启动迁移、日志或数据库写入失败。

## 推荐的离线备份

下面流程会产生短暂停机，但能让数据库、WAL/SHM、运行设置和 CookieCloud 文件处于同一个文件快照中。`mv`、`tar` 和 `docker stop/start` 都是管理员主动操作，执行前请确认变量指向正确的 AutoList 数据目录。

```bash
APP_ROOT=/mnt/user/appdata/Autolist
DATA_DIR="$APP_ROOT/data"
BACKUP_DIR="$APP_ROOT/backups"
STAMP=$(date +%Y%m%d%H%M%S)
ARCHIVE="$BACKUP_DIR/autolist-data-$STAMP.tar.gz"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
docker exec Autolist python -c 'import sqlite3; db=sqlite3.connect("/data/playlist-autodown.db", timeout=5); result=db.execute("PRAGMA integrity_check").fetchall(); assert result == [("ok",)], result; assert not db.execute("PRAGMA foreign_key_check").fetchall()'
docker stop Autolist
tar --xattrs --acls -czf "$ARCHIVE" -C "$APP_ROOT" data
chmod 600 "$ARCHIVE"
tar -tzf "$ARCHIVE" >/dev/null
docker start Autolist
```

启动后等待容器变为 `healthy`，再执行一次完整检查：

```bash
for i in $(seq 1 30); do
  STATUS=$(docker inspect --format '{{.State.Health.Status}}' Autolist 2>/dev/null || true)
  [ "$STATUS" = healthy ] && break
  [ "$STATUS" = unhealthy ] && { docker logs --tail 100 Autolist; exit 1; }
  sleep 2
done
docker inspect --format '{{.State.Health.Status}}' Autolist
docker exec Autolist python -c 'import sqlite3; db=sqlite3.connect("/data/playlist-autodown.db", timeout=5); result=db.execute("PRAGMA integrity_check").fetchall(); assert result == [("ok",)], result; assert not db.execute("PRAGMA foreign_key_check").fetchall(); print("SQLite integrity after backup: ok")'
```

若不能停容器，不要使用 `cp playlist-autodown.db` 做在线备份。应使用 SQLite Online Backup API 生成一致的数据库副本，并把运行设置、CookieCloud 和日志作为独立文件处理；数据库副本仍不能替代对整个 `/data` 的离线备份。

## 升级流程

1. 阅读新版本的 `README.md` 和本文件，确认是否有迁移说明。
2. 执行“升级前只读检查”和“推荐的离线备份”，确认压缩包可以列出且权限为 `0600`。
3. 保存当前镜像引用，保留本地 tag 部署方式。例如：

   ```bash
   docker image inspect autolist:1.03 --format '{{.Id}} {{.Created}}'
   docker image tag autolist:1.03 autolist:1.03-before-upgrade
   ```

4. 在不改变 `/mnt/user/appdata/Autolist/data` 挂载的情况下重新构建本地 `autolist:1.03`，或按 Unraid 模板重新创建唯一的 AutoList 容器。
5. 等待 Docker healthcheck 为 `healthy`，再执行 `/api/health`、SQLite `integrity_check` 和 `foreign_key_check`。
6. 只读确认片单数量、候选数量、历史数量和运行设置仍符合升级前记录；之后再进行页面验收。不要把真实下载提交作为升级健康检查的一部分。

如果升级后的迁移失败，先保留容器日志和当前数据目录，不要继续反复启动覆盖现场。停止容器后，将当前 `data` 改名保存，再从已验证的备份归档恢复；镜像回滚使用升级前保留的 image tag。整个过程中不要执行 `rm -rf` 删除备份或数据目录。

## 恢复流程

以下示例把当前数据目录改名保存，因此恢复失败时仍有回退副本。`ARCHIVE` 必须是经过 `tar -tzf` 校验、来源可信的 AutoList 备份：

```bash
APP_ROOT=/mnt/user/appdata/Autolist
ARCHIVE="$APP_ROOT/backups/autolist-data-YYYYMMDDHHMMSS.tar.gz"
STAMP=$(date +%Y%m%d%H%M%S)

test -f "$ARCHIVE"
tar -tzf "$ARCHIVE" >/dev/null
docker stop Autolist
mv "$APP_ROOT/data" "$APP_ROOT/data-before-restore-$STAMP"
tar --xattrs --acls -xzf "$ARCHIVE" -C "$APP_ROOT"
chown -R 10001:10001 "$APP_ROOT/data"
docker start Autolist
```

恢复后必须完成以下检查：

```bash
for i in $(seq 1 30); do
  STATUS=$(docker inspect --format '{{.State.Health.Status}}' Autolist 2>/dev/null || true)
  [ "$STATUS" = healthy ] && break
  [ "$STATUS" = unhealthy ] && { docker logs --tail 100 Autolist; exit 1; }
  sleep 2
done
docker inspect --format '{{.State.Health.Status}}' Autolist
curl --fail --silent http://127.0.0.1:8585/api/health
docker exec Autolist python -c 'import sqlite3; db=sqlite3.connect("/data/playlist-autodown.db", timeout=5); result=db.execute("PRAGMA integrity_check").fetchall(); assert result == [("ok",)], result; assert not db.execute("PRAGMA foreign_key_check").fetchall(); print("SQLite integrity after restore: ok")'
```

确认页面和数据无误后，再按管理员的数据保留策略处理 `data-before-restore-*`；在确认恢复成功前不要删除它。若应用启动后仍提示权限错误，停止容器并检查 `stat -c '%u:%g %a %n'`，不要通过改成 root 运行容器来绕过问题。

## 备份保留建议

至少保留最近 3 个可验证归档，并在重大版本升级前额外保留一个升级前归档。定期在隔离的临时目录做一次恢复演练，记录归档名称、数据库完整性结果、容器镜像 ID 和恢复时间；演练不得连接真实 TMDB、PT、Emby、Transmission 或 MoviePilot，也不得提交下载。
