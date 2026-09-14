#!/usr/bin/env bash
# Docker 离线安装(aarch64 / RK3588 Ubuntu): 静态二进制 + systemd 服务 + compose 插件
# 幂等: docker 已存在(PATH 或常见安装位置)时直接成功返回, 不做任何改动
# 用法: ./install_docker_offline.sh [bundle_dir]    (默认: 本脚本同级 bundle/)
# 来源: docker 官方静态包 download.docker.com/linux/static/stable/aarch64 (29.7.2)
#       compose 官方静态二进制 github.com/docker/compose (v5.4.0)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_DIR="${1:-$SCRIPT_DIR/bundle}"

SUDO=""
[[ $EUID -ne 0 ]] && SUDO="sudo"

fail() { echo "[install-docker] ✗ $1"; exit 1; }
ok()   { echo "[install-docker] ✓ $1"; }

# 1) 已存在? (PATH + 常见安装位置)
if command -v docker >/dev/null 2>&1; then
  ok "docker 已存在: $(command -v docker) ($(docker --version 2>/dev/null))"
  exit 0
fi
for d in /usr/bin /usr/local/bin /snap/bin /usr/local/sbin /opt/bin; do
  if [[ -x "$d/docker" ]]; then
    export PATH="$d:$PATH"
    ok "docker 不在 PATH, 已在 $d/docker 找到(无需安装)"
    exit 0
  fi
done

echo "[install-docker] 未找到 docker → 开始离线安装(aarch64 静态版)"
[[ "$(uname -m)" == "aarch64" ]] || fail "本机架构 $(uname -m) 不是 aarch64, 本 bundle 仅提供 aarch64 docker"
DOCKER_TGZ="$(ls "$BUNDLE_DIR"/docker-*-aarch64.tgz 2>/dev/null | head -1)"
COMPOSE_GZ="$BUNDLE_DIR/docker-compose-aarch64.gz"
[[ -n "$DOCKER_TGZ" && -f "$DOCKER_TGZ" ]] || fail "缺少 $BUNDLE_DIR/docker-*-aarch64.tgz"
[[ -f "$COMPOSE_GZ" ]] || fail "缺少 $COMPOSE_GZ"
command -v systemctl >/dev/null 2>&1 || fail "无 systemd/systemctl, 无法注册 docker 服务"
if [[ -n "$SUDO" ]]; then $SUDO -v 2>/dev/null || fail "需要 sudo 权限(写入 /usr/bin、/etc/systemd)"; fi

# 2) 二进制 → /usr/bin
TMP="$(mktemp -d)"
tar xzf "$DOCKER_TGZ" -C "$TMP" || fail "解压 $DOCKER_TGZ 失败(文件损坏?)"
$SUDO cp -f "$TMP"/docker/* /usr/bin/ || fail "复制二进制到 /usr/bin 失败"
ok "二进制已安装: $(docker --version 2>/dev/null)"

# 3) systemd 服务(containerd + dockerd)
$SUDO tee /etc/systemd/system/containerd.service >/dev/null <<'UNIT'
[Unit]
Description=containerd container runtime
Documentation=https://containerd.io
After=network.target local-fs.target

[Service]
ExecStartPre=-/sbin/modprobe overlay
ExecStart=/usr/bin/containerd
Type=notify
Delegate=yes
KillMode=process
Restart=always
RestartSec=5
LimitNPROC=infinity
LimitCORE=infinity
LimitNOFILE=infinity
TasksMax=infinity
OOMScoreAdjust=-999

[Install]
WantedBy=multi-user.target
UNIT

$SUDO tee /etc/systemd/system/docker.service >/dev/null <<'UNIT'
[Unit]
Description=Docker Application Container Engine
Documentation=https://docs.docker.com
After=network-online.target containerd.service
Wants=network-online.target
Requires=containerd.service

[Service]
Type=notify
ExecStart=/usr/bin/dockerd --containerd=/run/containerd/containerd.sock
ExecReload=/bin/kill -s HUP $MAINPID
Restart=always
RestartSec=5
LimitNOFILE=infinity
LimitNPROC=infinity
LimitCORE=infinity
TasksMax=infinity
Delegate=yes
KillMode=process
OOMScoreAdjust=-999

[Install]
WantedBy=multi-user.target
UNIT

$SUDO systemctl daemon-reload
$SUDO systemctl enable --now containerd || fail "containerd 启动失败 → journalctl -u containerd -n 50"
$SUDO systemctl enable --now docker     || fail "docker 启动失败 → journalctl -u docker -n 50"
ok "containerd + docker 服务已注册并启动"

# 4) compose 插件(系统级, 两个标准路径都放)
gzip -dc "$COMPOSE_GZ" > "$TMP/docker-compose" || fail "解压 compose 失败"
for pdir in /usr/libexec/docker/cli-plugins /usr/lib/docker/cli-plugins; do
  $SUDO mkdir -p "$pdir"
  $SUDO cp -f "$TMP/docker-compose" "$pdir/docker-compose"
  $SUDO chmod +x "$pdir/docker-compose"
done
ok "docker compose 插件已安装"

# 5) 验证
docker version >/dev/null 2>&1 || fail "docker daemon 未响应 → journalctl -u docker -n 50"
docker compose version >/dev/null 2>&1 || fail "docker compose 不可用"
ok "安装完成: $(docker --version) / $(docker compose version)"
