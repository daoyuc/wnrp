//! phpvm GUI 可行性原型（iced 0.14）
//!
//! 验证目标（对应调研里 GUI 重写的真实难点）：
//!   1. 页签容器        —— iced 无内置 Tabs，用按钮行 + 状态切换自绘
//!   2. 表格            —— 内置 table 控件（0.14 新增，取代 ttk.Treeview）
//!   3. 行内操作按钮    —— 表格单元格里放 button，验证 Message 能否回传
//!   4. 定时刷新        —— subscription + time::every（对应 GUI 的轮询刷新）
//!   5. 日志输出        —— scrollable + anchor_bottom（日志自动贴底）
//!   6. 主题切换        —— Theme::Dark / Light（对应 ui/theme.py）
//!   7. i18n 切换       —— 直接读仓库现有 i18n/*.json，验证资源可零改动复用
//!   8. 系统托盘        —— iced 无内置，需 tray-icon 等第三方 crate（本原型不含）

use iced::time;
use iced::widget::{button, column, container, pick_list, row, scrollable, table, text};
use iced::{Element, Length, Subscription, Task, Theme};
use serde_json::Value;
use std::collections::HashMap;
use std::path::Path;
use std::time::Duration;

// ---------------------------------------------------------------- 领域模型

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Tab {
    Services,
    Sites,
    Logs,
}

impl Tab {
    const ALL: [Tab; 3] = [Tab::Services, Tab::Sites, Tab::Logs];
    fn label(&self) -> &'static str {
        match self {
            Tab::Services => "服务",
            Tab::Sites => "站点映射",
            Tab::Logs => "日志",
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Lang {
    ZhCn,
    En,
    Ja,
}

impl Lang {
    const ALL: [Lang; 3] = [Lang::ZhCn, Lang::En, Lang::Ja];
    fn label(&self) -> &'static str {
        match self {
            Lang::ZhCn => "简体中文",
            Lang::En => "English",
            Lang::Ja => "日本語",
        }
    }
    /// zh_CN 是源码原文、无资源文件（与 core/i18n.py 的回落语义一致）
    fn code(&self) -> Option<&'static str> {
        match self {
            Lang::ZhCn => None,
            Lang::En => Some("en"),
            Lang::Ja => Some("ja"),
        }
    }
}

// pick_list 要求选项实现 Display（下拉列表的展示文本）
impl std::fmt::Display for Lang {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.label())
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Scheme {
    Light,
    Dark,
}

impl Scheme {
    const ALL: [Scheme; 2] = [Scheme::Light, Scheme::Dark];
    fn label(&self) -> &'static str {
        match self {
            Scheme::Light => "浅色",
            Scheme::Dark => "深色",
        }
    }
}

impl std::fmt::Display for Scheme {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.label())
    }
}

#[derive(Debug, Clone)]
struct Service {
    id: usize,
    name: String,
    port: u16,
    running: bool,
}

// ---------------------------------------------------------------- 状态与消息

#[derive(Debug, Clone)]
enum Message {
    SelectTab(Tab),
    ToggleService(usize),
    Tick,
    SelectLang(Lang),
    SelectScheme(Scheme),
}

struct App {
    tab: Tab,
    services: Vec<Service>,
    logs: Vec<String>,
    lang: Lang,
    scheme: Scheme,
    /// 语言代码 -> (msgid -> 译文)
    i18n: HashMap<String, HashMap<String, String>>,
    ticks: usize,
}

impl App {
    fn new() -> Self {
        let mut i18n = HashMap::new();
        for lang in Lang::ALL {
            if let Some(code) = lang.code() {
                let table = load_lang(code);
                eprintln!("[i18n] {code}: 加载 {} 条译文", table.len());
                i18n.insert(code.to_string(), table);
            }
        }

        Self {
            tab: Tab::Services,
            services: vec![
                Service { id: 0, name: "php81".into(), port: 9081, running: false },
                Service { id: 1, name: "php85".into(), port: 9001, running: true },
                Service { id: 2, name: "nginx".into(), port: 80, running: true },
            ],
            logs: vec!["[boot] phpvm iced 原型已启动".to_string()],
            lang: Lang::ZhCn,
            scheme: Scheme::Dark,
            i18n,
            ticks: 0,
        }
    }

    /// 复刻 core/i18n.py 的 translate：查不到就回落原文
    fn t(&self, msgid: &str) -> String {
        match self.lang.code() {
            None => msgid.to_string(),
            Some(code) => self
                .i18n
                .get(code)
                .and_then(|m| m.get(msgid))
                .cloned()
                .unwrap_or_else(|| msgid.to_string()),
        }
    }

    fn log(&mut self, line: impl Into<String>) {
        self.logs.push(line.into());
        if self.logs.len() > 500 {
            self.logs.remove(0);
        }
    }
}

// ---------------------------------------------------------------- 逻辑

fn update(app: &mut App, message: Message) -> Task<Message> {
    match message {
        Message::SelectTab(tab) => app.tab = tab,
        Message::ToggleService(id) => {
            if let Some(svc) = app.services.iter_mut().find(|s| s.id == id) {
                svc.running = !svc.running;
                let state = if svc.running { "启动" } else { "停止" };
                let line = format!("[{}] {} :{}", state, svc.name, svc.port);
                app.log(line);
            }
        }
        Message::Tick => {
            app.ticks += 1;
            app.log(format!("[tick {}] 轮询刷新（模拟状态刷新）", app.ticks));
        }
        Message::SelectLang(lang) => {
            app.lang = lang;
            app.log(format!("[i18n] 切换语言 -> {}", lang.label()));
        }
        Message::SelectScheme(scheme) => app.scheme = scheme,
    }
    Task::none()
}

fn subscription(_app: &App) -> Subscription<Message> {
    time::every(Duration::from_secs(2)).map(|_| Message::Tick)
}

// ---------------------------------------------------------------- 视图

fn view(app: &App) -> Element<Message> {
    let header = row![
        text("phpvm · iced 原型").size(20),
        text(format!("tick={}", app.ticks)).size(12),
    ]
    .spacing(12)
    .align_y(iced::Alignment::Center);

    let tabs = row(Tab::ALL.map(|tab| {
        let label = text(tab.label()).size(14);
        if tab == app.tab {
            button(label).on_press(Message::SelectTab(tab)).into()
        } else {
            button(label).on_press(Message::SelectTab(tab)).style(button::secondary).into()
        }
    }))
    .spacing(6);

    let settings = row![
        pick_list(Lang::ALL, Some(app.lang), Message::SelectLang).placeholder("语言"),
        pick_list(Scheme::ALL, Some(app.scheme), Message::SelectScheme).placeholder("主题"),
    ]
    .spacing(8);

    let body = match app.tab {
        Tab::Services => services_view(app),
        Tab::Sites => sites_view(app),
        Tab::Logs => logs_view(app),
    };

    container(column![header, tabs, settings, body].spacing(10).padding(14))
        .width(Length::Fill)
        .height(Length::Fill)
        .into()
}

fn services_view(app: &App) -> Element<Message> {
    let columns = vec![
        table::column(text(app.t("服务")), |s: Service| text(s.name.clone())),
        table::column(text(app.t("端口")), |s: Service| text(s.port.to_string())),
        table::column(text(app.t("状态")), |s: Service| {
            text(if s.running { app.t("运行中") } else { app.t("已停止") })
        }),
        table::column(text(app.t("操作")), |s: Service| {
            let label = if s.running { app.t("停止") } else { app.t("启动") };
            button(text(label).size(12)).on_press(Message::ToggleService(s.id))
        }),
    ];

    table(columns, app.services.clone())
        .width(Length::Fill)
        .separator(1.0)
        .into()
}

fn sites_view(app: &App) -> Element<Message> {
    // 站点映射页同样是多列表格（域名 / 根目录 / PHP 版本 / 端口），此处仅示意
    column![
        text(app.t("站点映射")).size(16),
        text("（vhost 面板与 php 面板同构：均为多列表格 + 行内操作，故不再重复验证）"),
    ]
    .spacing(8)
    .into()
}

fn logs_view(app: &App) -> Element<Message> {
    scrollable(
        column(app.logs.iter().map(|l| text(l.clone()).size(12).into()).collect::<Vec<Element<Message>>>())
            .spacing(2),
    )
    .anchor_bottom()
    .width(Length::Fill)
    .height(Length::Fill)
    .into()
}

// ---------------------------------------------------------------- i18n 资源加载

/// 直接读仓库既有的 i18n/<lang>/*.json —— 验证资源文件能否零改动复用
fn load_lang(code: &str) -> HashMap<String, String> {
    let mut map = HashMap::new();
    let base = Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("../../i18n")
        .join(code);

    let entries = match std::fs::read_dir(&base) {
        Ok(e) => e,
        Err(_) => return map,
    };

    for entry in entries.flatten() {
        let path = entry.path();
        if path.extension().and_then(|s| s.to_str()) != Some("json") {
            continue;
        }
        let Ok(raw) = std::fs::read_to_string(&path) else {
            continue;
        };
        if let Ok(Value::Object(obj)) = serde_json::from_str::<Value>(&raw) {
            for (k, v) in obj {
                if let Value::String(s) = v {
                    map.insert(k, s);
                }
            }
        }
    }
    map
}

// ---------------------------------------------------------------- 入口

fn main() -> iced::Result {
    iced::application(App::new, update, view)
        .subscription(subscription)
        .theme(|app: &App| match app.scheme {
            Scheme::Dark => Theme::Dark,
            Scheme::Light => Theme::Light,
        })
        .window_size((880.0, 560.0))
        .run()
}
