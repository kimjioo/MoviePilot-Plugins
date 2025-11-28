"""
DeepFlood论坛签到插件
版本: 1.1.0
作者: Madrays (Modified for DeepFlood)
功能:
- 自动完成DeepFlood论坛每日签到
- 支持选择随机奖励或固定奖励
- 自动失败重试机制
- 定时签到和历史记录
- 支持绕过CloudFlare防护
"""
import time
import random
import traceback
from datetime import datetime, timedelta

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from app.schemas import NotificationType
import requests
from urllib.parse import urlencode
import json

# cloudscraper 作为 Cloudflare 备用方案
try:
    import cloudscraper
    HAS_CLOUDSCRAPER = True
except Exception:
    HAS_CLOUDSCRAPER = False

# 尝试导入curl_cffi库，用于绕过CloudFlare防护
try:
    from curl_cffi import requests as curl_requests
    HAS_CURL_CFFI = True
except ImportError:
    HAS_CURL_CFFI = False


class deepfloodsign(_PluginBase):
    # 插件名称
    plugin_name = "DeepFlood论坛签到"
    # 插件描述
    plugin_desc = "自动完成DeepFlood论坛每日签到，支持随机奖励和自动重试功能"
    # 插件图标 (建议替换为DeepFlood的图标)
    plugin_icon = "https://www.deepflood.com/static/logo.png"
    # 插件版本
    plugin_version = "2.1.0"
    # 插件作者
    plugin_author = "madrays"
    # 作者主页
    author_url = "https://github.com/madrays"
    # 插件配置项ID前缀
    plugin_config_prefix = "deepfloodsign_"
    # 加载顺序
    plugin_order = 1
    # 可使用的用户级别
    auth_level = 2

    # 私有属性
    _enabled = False
    _cookie = None
    _notify = False
    _onlyonce = False
    _clear_history = False  # 新增：是否清除历史记录
    _cron = None
    _random_choice = True  # 是否选择随机奖励，否则选择固定奖励
    _history_days = 30  # 历史保留天数
    _use_proxy = True     # 是否使用代理，默认启用
    _max_retries = 3      # 最大重试次数
    _retry_count = 0      # 当天重试计数
    _scheduled_retry = None  # 计划的重试任务
    _verify_ssl = False    # 是否验证SSL证书，默认禁用
    _min_delay = 5         # 请求前最小随机等待（秒）
    _max_delay = 12        # 请求前最大随机等待（秒）
    _member_id = ""       # DeepFlood 成员ID（可选，用于获取用户信息）
    _stats_days = 30

    _scraper = None        # cloudscraper 实例

    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None
    _manual_trigger = False

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        logger.info("============= deepfloodsign 初始化 =============")
        try:
            if config:
                self._enabled = config.get("enabled")
                self._cookie = config.get("cookie")
                self._notify = config.get("notify")
                self._cron = config.get("cron")
                self._onlyonce = config.get("onlyonce")
                self._random_choice = config.get("random_choice")
                # 确保数值类型配置的安全性
                try:
                    self._history_days = int(config.get("history_days", 30))
                except (ValueError, TypeError):
                    self._history_days = 30
                    logger.warning("history_days 配置无效，使用默认值 30")
                
                self._use_proxy = config.get("use_proxy", True)
                
                try:
                    self._max_retries = int(config.get("max_retries", 3))
                except (ValueError, TypeError):
                    self._max_retries = 3
                    logger.warning("max_retries 配置无效，使用默认值 3")
                
                self._verify_ssl = config.get("verify_ssl", False)
                
                try:
                    self._min_delay = int(config.get("min_delay", 5))
                except (ValueError, TypeError):
                    self._min_delay = 5
                    logger.warning("min_delay 配置无效，使用默认值 5")
                
                try:
                    self._max_delay = int(config.get("max_delay", 12))
                except (ValueError, TypeError):
                    self._max_delay = 12
                    logger.warning("max_delay 配置无效，使用默认值 12")
                self._member_id = (config.get("member_id") or "").strip()
                self._clear_history = config.get("clear_history", False) # 初始化清除历史记录
                try:
                    self._stats_days = int(config.get("stats_days", 30))
                except (ValueError, TypeError):
                    self._stats_days = 30
                
                logger.info(f"配置: enabled={self._enabled}, notify={self._notify}, cron={self._cron}, "
                           f"random_choice={self._random_choice}, history_days={self._history_days}, "
                           f"use_proxy={self._use_proxy}, max_retries={self._max_retries}, verify_ssl={self._verify_ssl}, "
                           f"min_delay={self._min_delay}, max_delay={self._max_delay}, member_id={self._member_id or '未设置'}, clear_history={self._clear_history}")
                # 初始化 cloudscraper（可选，用于绕过 Cloudflare）
                if HAS_CLOUDSCRAPER:
                    try:
                        self._scraper = cloudscraper.create_scraper(browser="chrome")
                    except Exception:
                        try:
                            self._scraper = cloudscraper.create_scraper()
                        except Exception as e2:
                            logger.warning(f"cloudscraper 初始化失败: {str(e2)}")
                            self._scraper = None
                    if self._scraper:
                        proxies = self._get_proxies()
                        if proxies:
                            self._scraper.proxies = proxies
                            logger.info(f"cloudscraper 初始化代理: {self._scraper.proxies}")
                        logger.info("cloudscraper 初始化成功")
            
            if self._onlyonce:
                logger.info("执行一次性签到")
                self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                self._manual_trigger = True
                self._scheduler.add_job(func=self.sign, trigger='date',
                                   run_date=datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(seconds=3),
                                   name="DeepFlood论坛签到")
                self._onlyonce = False
                self.update_config({
                    "onlyonce": False,
                    "enabled": self._enabled,
                    "cookie": self._cookie,
                    "notify": self._notify,
                    "cron": self._cron,
                    "random_choice": self._random_choice,
                    "history_days": self._history_days,
                    "use_proxy": self._use_proxy,
                    "max_retries": self._max_retries,
                    "verify_ssl": self._verify_ssl,
                    "min_delay": self._min_delay,
                    "max_delay": self._max_delay,
                    "member_id": self._member_id,
                    "clear_history": self._clear_history,
                    "stats_days": self._stats_days
                })

                # 启动任务
                if self._scheduler.get_jobs():
                    self._scheduler.print_jobs()
                    self._scheduler.start()

                # 如果需要清除历史记录，则清空
                if self._clear_history:
                    logger.info("检测到清除历史记录标志，开始清空数据...")
                    self.clear_sign_history()
                    logger.info("已清除签到历史记录")
                    # 保存配置，将 clear_history 设置为 False
                    self.update_config({
                        "onlyonce": False,
                        "enabled": self._enabled,
                        "cookie": self._cookie,
                        "notify": self._notify,
                        "cron": self._cron,
                        "random_choice": self._random_choice,
                        "history_days": self._history_days,
                        "use_proxy": self._use_proxy,
                        "max_retries": self._max_retries,
                        "verify_ssl": self._verify_ssl,
                        "min_delay": self._min_delay,
                        "max_delay": self._max_delay,
                        "member_id": self._member_id,
                        "clear_history": False,
                        "stats_days": self._stats_days
                    })
                    logger.info("已保存配置，clear_history 已重置为 False")

        except Exception as e:
            logger.error(f"deepfloodsign初始化错误: {str(e)}", exc_info=True)

    def sign(self):
        """
        执行DeepFlood签到
        """
        logger.info("============= 开始DeepFlood签到 =============")
        sign_dict = None
        
        try:
            # 检查Cookie
            if not self._cookie:
                logger.error("未配置Cookie")
                sign_dict = {
                    "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": "签到失败: 未配置Cookie",
                }
                self._save_sign_history(sign_dict)
                
                if self._notify:
                    self.post_message(
                        mtype=NotificationType.SiteMessage,
                        title="【DeepFlood论坛签到失败】",
                        text="未配置Cookie，请在设置中添加Cookie"
                    )
                return sign_dict
            
            # 请求前随机等待
            self._wait_random_interval()
            
            # 无论任何情况都尝试执行API签到
            result = self._run_api_sign()
            
            # 始终获取最新用户信息
            user_info = None
            try:
                if getattr(self, "_member_id", ""):
                    user_info = self._fetch_user_info(self._member_id)
            except Exception as e:
                logger.warning(f"获取用户信息失败: {str(e)}")
            
            # 始终获取签到记录以获取奖励和排名
            attendance_record = None
            try:
                attendance_record = self._fetch_attendance_record()
            except Exception as e:
                logger.warning(f"获取签到记录失败: {str(e)}")
            
            # 处理签到结果
            if result["success"]:
                # 保存签到记录（包含奖励信息）
                sign_dict = {
                    "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": "签到成功" if not result.get("already_signed") else "已签到",
                    "message": result.get("message", "")
                }
                
                # 添加奖励信息到历史记录
                if attendance_record and attendance_record.get("gain"):
                    sign_dict["gain"] = attendance_record.get("gain")
                    if attendance_record.get("rank"):
                        sign_dict["rank"] = attendance_record.get("rank")
                        sign_dict["total_signers"] = attendance_record.get("total_signers")
                elif result.get("gain"):
                    sign_dict["gain"] = result.get("gain")
                
                self._save_sign_history(sign_dict)
                self._save_last_sign_date()
                # 重置重试计数
                self._retry_count = 0

                # 发送通知
                if self._notify:
                    try:
                        self._send_sign_notification(sign_dict, result, user_info, attendance_record)
                        logger.info("签到成功通知发送成功")
                    except Exception as e:
                        logger.error(f"签到成功通知发送失败: {str(e)}")
                        # 通知失败不影响主流程，继续执行
                try:
                    stats = self._get_signin_stats(self._stats_days)
                    if stats:
                        self.save_data('last_signin_stats', stats)
                except Exception as e:
                    logger.warning(f"获取收益统计失败: {str(e)}")
            else:
                # 签到失败，安排重试
                sign_dict = {
                    "date": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                    "status": "签到失败",
                    "message": result.get("message", "")
                }
                
                # 最后兜底：通过签到记录进行时间验证或当日确认
                try:
                    if attendance_record and attendance_record.get("created_at"):
                        record_date = datetime.fromisoformat(attendance_record["created_at"].replace('Z', '+00:00'))
                        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
                        if record_date.date() == today.date():
                            logger.info(f"从签到记录确认今日已签到: {attendance_record}")
                            result["success"] = True
                            result["already_signed"] = True
                            result["message"] = "今日已签到（记录确认）"
                            sign_dict["status"] = "已签到（记录确认）"
                        else:
                            # 兜底时间验证：仅当无其它成功信号时，且时间差极小才认为成功
                            current_time = datetime.utcnow()
                            record_time = datetime.fromisoformat(attendance_record["created_at"].replace('Z', '+00:00')).replace(tzinfo=None)
                            time_diff = abs((current_time - record_time).total_seconds() / 3600)
                            logger.info(f"兜底时间验证差值: {time_diff:.2f}h")
                            if time_diff < 0.5:
                                logger.info("时间差 < 0.5h，作为最后兜底判定为成功")
                                result["success"] = True
                                result["signed"] = True
                                sign_dict["status"] = "签到成功（兜底时间验证）"
                                result["message"] = "签到成功（兜底时间验证）"
                    else:
                        logger.info("无有效签到记录用于兜底")
                except Exception as e:
                    logger.warning(f"兜底时间验证失败: {str(e)}")
                
                # 保存历史记录（包括可能通过兜底更改的状态）
                self._save_sign_history(sign_dict)
                try:
                    stats = self._get_signin_stats(self._stats_days)
                    if stats:
                        self.save_data('last_signin_stats', stats)
                except Exception as e:
                    logger.warning(f"获取收益统计失败: {str(e)}")
                
                # 检查是否需要重试
                # 确保 _max_retries 是整数类型
                max_retries = int(self._max_retries) if self._max_retries is not None else 0
                
                if max_retries and self._retry_count < max_retries:
                    self._retry_count += 1
                    retry_minutes = random.randint(5, 15)
                    retry_time = datetime.now(tz=pytz.timezone(settings.TZ)) + timedelta(minutes=retry_minutes)
                    
                    logger.info(f"签到失败，将在 {retry_minutes} 分钟后重试 (重试 {self._retry_count}/{max_retries})")
                    
                    # 安排重试任务
                    if not self._scheduler:
                        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
                        if not self._scheduler.running:
                            self._scheduler.start()
                    
                    # 移除之前计划的重试任务（如果有）
                    if self._scheduled_retry:
                        try:
                            self._scheduler.remove_job(self._scheduled_retry)
                        except Exception as e:
                            # 忽略移除不存在任务的错误
                            logger.warning(f"移除旧任务时出错 (可忽略): {str(e)}")
                    
                    # 添加新的重试任务
                    self._scheduled_retry = f"deepflood_retry_{int(time.time())}"
                    self._scheduler.add_job(
                        func=self.sign,
                        trigger='date',
                        run_date=retry_time,
                        id=self._scheduled_retry,
                    name=f"DeepFlood论坛签到重试 {self._retry_count}/{max_retries}"
                    )
                    
                    if self._notify:
                        self.post_message(
                            mtype=NotificationType.SiteMessage,
                            title="【DeepFlood论坛签到失败】",
                            text=f"签到失败: {result.get('message', '未知错误')}\n将在 {retry_minutes} 分钟后进行第 {self._retry_count}/{max_retries} 次重试\n⏱️ {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                        )
                else:
                    # 达到最大重试次数或未配置重试
                    if max_retries == 0:
                        logger.info("未配置自动重试 (max_retries=0)，本次结束")
                    else:
                        logger.warning(f"已达到最大重试次数 ({max_retries})，今日不再重试")
                    
                    if self._notify:
                        retry_text = "未配置自动重试" if max_retries == 0 else f"已达到最大重试次数 ({max_retries})"
                        self.post_message(
                            mtype=NotificationType.SiteMessage,
                            title="【DeepFlood论坛签到失败】",
                            text=f"签到失败: {result.get('message', '未知错误')}\
