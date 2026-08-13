//! `/prompts` modal — catalog, in-TUI edit, and named presets.
//!
//! Defaults always resolve from the binary templates (never snapshotted as
//! source of truth). User overrides and presets live only under `$GROK_HOME`.

use std::borrow::Cow;

use crossterm::event::{
    KeyCode, KeyEvent, KeyEventKind, KeyModifiers, MouseButton, MouseEvent, MouseEventKind,
};
use ratatui::buffer::Buffer;
use ratatui::layout::Rect;
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::StatefulWidgetRef;
use unicode_width::UnicodeWidthStr;
use xai_grok_agent::{
    PresetWriteMode, PromptCategory, PromptId, PromptPresetInfo, PromptSource, list_presets,
    list_prompts,
};
use xai_ratatui_textarea::{TextArea, TextAreaState};

use crate::app::actions::Action;
use crate::app::app_view::InputOutcome;
use crate::input::line_editor::{LineEditOutcome, LineEditor};
use crate::render::SafeBuf;
use crate::render::scrollbar::{render_scrollbar, scrollbar_click_to_offset};
use crate::scrollback::blocks::markdown_content::MarkdownContent;
use crate::theme::Theme;
use crate::views::modal_window::{
    self, ModalContentArea, ModalSizing, ModalWindowConfig, ModalWindowState, Shortcut,
};

const SPLIT_MIN_WIDTH: u16 = 80;
const LIST_WIDTH_RATIO: f64 = 0.40;
pub const MODAL_TITLE: &str = "Prompts";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PromptsTab {
    Catalog,
    Presets,
}

impl PromptsTab {
    const ALL: &[PromptsTab] = &[PromptsTab::Catalog, PromptsTab::Presets];
    fn label(self) -> &'static str {
        match self {
            Self::Catalog => "Catalog",
            Self::Presets => "Presets",
        }
    }
}

#[derive(Debug, Clone)]
pub struct PromptListEntry {
    pub id: Option<PromptId>,
    pub label: String,
    pub meta_display: String,
    pub is_header: bool,
    pub is_override: bool,
}

pub(crate) enum PromptsModalMode {
    Browse,
    FilterFocused,
    ConfirmingReset {
        idx: usize,
    },
    /// In-TUI multi-line editor for one catalog entry.
    Editing {
        id: PromptId,
        textarea: TextArea,
        textarea_state: TextAreaState,
        dirty: bool,
        original: String,
    },
    /// Name a preset: new snapshot, rename, or duplicate.
    NamingPreset {
        editor: LineEditor,
        error: Option<String>,
        intent: NamingIntent,
    },
    ConfirmingDeletePreset {
        name: String,
    },
    /// A new preset was named after an existing one: confirm the replacement.
    ConfirmingOverwritePreset {
        name: String,
    },
    /// Confirm running this session on pure built-in defaults.
    ConfirmingDefaults,
}

/// What the name being typed in [`PromptsModalMode::NamingPreset`] is for.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) enum NamingIntent {
    /// Snapshot the session's current overrides under a new name.
    New,
    /// Rename an existing preset.
    Rename(String),
    /// Copy an existing preset under a new name.
    Duplicate(String),
}

impl NamingIntent {
    fn title(&self) -> String {
        match self {
            Self::New => "Save current prompts as a new preset".to_string(),
            Self::Rename(from) => format!("Rename preset `{from}`"),
            Self::Duplicate(from) => format!("Duplicate preset `{from}`"),
        }
    }
}

pub struct PromptsModalState {
    pub window: ModalWindowState,
    pub tab: PromptsTab,
    pub entries: Vec<PromptListEntry>,
    pub presets: Vec<PromptPresetInfo>,
    pub selected: usize,
    pub scroll_offset: usize,
    pub preview_markdown: Option<MarkdownContent>,
    pub preview_scroll: usize,
    pub(crate) mode: PromptsModalMode,
    query: LineEditor,
    pub fullscreen: bool,
    filtered_cache: Vec<usize>,
    preview_total_lines: usize,
    list_area: Rect,
    preview_area: Rect,
    list_scrollbar_area: Option<Rect>,
    preview_scrollbar_area: Option<Rect>,
    pub status: Option<String>,
    /// Active preset name for the title bar subtitle.
    pub active_preset: Option<String>,
    /// Where this session reads its prompt bodies from.
    pub source: PromptSource,
    /// Last text-area rect while editing (mouse hit-test + cursor).
    pub edit_area: Option<Rect>,
    /// Hardware terminal caret `(x, y)` for the current edit surface, or
    /// `None` when not editing. Consumed by the agent frame path so the
    /// blinking `|` is shown (modals otherwise suppress the prompt caret).
    pub cursor_pos: Option<(u16, u16)>,
}

impl PromptsModalState {
    pub fn new() -> Self {
        let mut state = Self {
            window: ModalWindowState::new(),
            tab: PromptsTab::Catalog,
            entries: Vec::new(),
            presets: Vec::new(),
            selected: 0,
            scroll_offset: 0,
            preview_markdown: None,
            preview_scroll: 0,
            mode: PromptsModalMode::Browse,
            query: LineEditor::default(),
            fullscreen: false,
            filtered_cache: Vec::new(),
            preview_total_lines: 0,
            list_area: Rect::default(),
            preview_area: Rect::default(),
            list_scrollbar_area: None,
            preview_scrollbar_area: None,
            status: None,
            active_preset: None,
            source: PromptSource::Defaults,
            edit_area: None,
            cursor_pos: None,
        };
        state.reload_data();
        state.advance_past_headers();
        state.load_preview();
        state
    }

    /// Hardware caret for the agent frame when this modal owns typing focus.
    pub fn caret(&self) -> Option<(u16, u16)> {
        self.cursor_pos
    }

    pub fn refresh(&mut self) {
        let selected_id = self.selected_entry().and_then(|e| e.id);
        let selected_preset = self.selected_preset_name();
        let tab = self.tab;
        self.reload_data();
        self.tab = tab;
        self.invalidate_filter();
        match self.tab {
            PromptsTab::Catalog => {
                if let Some(id) = selected_id {
                    if let Some((filt_idx, _)) = self
                        .filtered_cache
                        .iter()
                        .enumerate()
                        .find(|&(_, &orig)| self.entries[orig].id == Some(id))
                    {
                        self.selected = filt_idx;
                    } else {
                        self.advance_past_headers();
                    }
                } else {
                    self.clamp_selected();
                }
            }
            PromptsTab::Presets => {
                if let Some(name) = selected_preset {
                    if let Some(i) = self.presets.iter().position(|p| p.name == name) {
                        self.selected = i;
                    } else {
                        self.selected = 0;
                    }
                } else {
                    self.selected = 0;
                }
            }
        }
        if !matches!(self.mode, PromptsModalMode::Editing { .. }) {
            self.load_preview();
        }
    }

    fn reload_data(&mut self) {
        self.entries = build_catalog_entries();
        self.presets = list_presets().unwrap_or_default();
        self.source = xai_grok_agent::session_source();
        self.active_preset = self.source.preset_name().map(str::to_string);
        self.invalidate_filter();
    }

    pub fn query(&self) -> &str {
        self.query.text()
    }

    fn invalidate_filter(&mut self) {
        match self.tab {
            PromptsTab::Catalog => {
                self.filtered_cache = compute_filtered(&self.entries, self.query());
            }
            PromptsTab::Presets => {
                self.filtered_cache = compute_filtered_presets(&self.presets, self.query());
            }
        }
    }

    pub fn selected_entry(&self) -> Option<&PromptListEntry> {
        if self.tab != PromptsTab::Catalog {
            return None;
        }
        self.filtered_cache
            .get(self.selected)
            .and_then(|&i| self.entries.get(i))
    }

    fn selected_preset(&self) -> Option<&PromptPresetInfo> {
        if self.tab != PromptsTab::Presets {
            return None;
        }
        self.filtered_cache
            .get(self.selected)
            .and_then(|&i| self.presets.get(i))
    }

    fn selected_preset_name(&self) -> Option<String> {
        self.selected_preset().map(|p| p.name.clone())
    }

    fn switch_tab(&mut self, tab: PromptsTab) {
        if self.tab == tab {
            return;
        }
        // Don't switch away mid-edit without cancel.
        if matches!(self.mode, PromptsModalMode::Editing { .. }) {
            return;
        }
        self.tab = tab;
        self.mode = PromptsModalMode::Browse;
        self.query.set_text("");
        self.scroll_offset = 0;
        self.selected = 0;
        self.invalidate_filter();
        if tab == PromptsTab::Catalog {
            self.advance_past_headers();
        }
        self.load_preview();
    }

    fn advance_past_headers(&mut self) {
        if self.tab != PromptsTab::Catalog {
            return;
        }
        let filtered = &self.filtered_cache;
        for (i, &orig) in filtered.iter().enumerate() {
            if !self.entries[orig].is_header {
                self.selected = i;
                return;
            }
        }
    }

    pub fn select_next(&mut self) {
        if self.advance_next() {
            self.load_preview();
        }
    }

    pub fn select_prev(&mut self) {
        if self.advance_prev() {
            self.load_preview();
        }
    }

    fn advance_next(&mut self) -> bool {
        match self.tab {
            PromptsTab::Catalog => {
                let filtered = &self.filtered_cache;
                let mut next = self.selected + 1;
                while next < filtered.len() {
                    if !self.entries[filtered[next]].is_header {
                        self.selected = next;
                        return true;
                    }
                    next += 1;
                }
                false
            }
            PromptsTab::Presets => {
                if self.selected + 1 < self.filtered_cache.len() {
                    self.selected += 1;
                    true
                } else {
                    false
                }
            }
        }
    }

    fn advance_prev(&mut self) -> bool {
        match self.tab {
            PromptsTab::Catalog => {
                if self.selected == 0 {
                    return false;
                }
                let filtered = &self.filtered_cache;
                let mut prev = self.selected - 1;
                loop {
                    if !self.entries[filtered[prev]].is_header {
                        self.selected = prev;
                        return true;
                    }
                    if prev == 0 {
                        break;
                    }
                    prev -= 1;
                }
                false
            }
            PromptsTab::Presets => {
                if self.selected == 0 {
                    false
                } else {
                    self.selected -= 1;
                    true
                }
            }
        }
    }

    pub fn select_at(&mut self, filt_idx: usize) -> bool {
        match self.tab {
            PromptsTab::Catalog => {
                let filtered = &self.filtered_cache;
                if filt_idx >= filtered.len() {
                    return false;
                }
                if self.entries[filtered[filt_idx]].is_header {
                    return false;
                }
                if self.selected == filt_idx {
                    return false;
                }
                self.selected = filt_idx;
                self.load_preview();
                true
            }
            PromptsTab::Presets => {
                if filt_idx >= self.filtered_cache.len() || self.selected == filt_idx {
                    return false;
                }
                self.selected = filt_idx;
                self.load_preview();
                true
            }
        }
    }

    pub fn clamp_selected(&mut self) {
        match self.tab {
            PromptsTab::Catalog => {
                let filtered = &self.filtered_cache;
                if filtered.is_empty() {
                    self.selected = 0;
                    self.preview_markdown = None;
                    return;
                }
                if self.selected >= filtered.len() {
                    self.selected = filtered.len() - 1;
                }
                if self.entries[filtered[self.selected]].is_header {
                    self.advance_past_headers();
                }
                self.load_preview();
            }
            PromptsTab::Presets => {
                if self.filtered_cache.is_empty() {
                    self.selected = 0;
                    self.preview_markdown = None;
                    return;
                }
                if self.selected >= self.filtered_cache.len() {
                    self.selected = self.filtered_cache.len() - 1;
                }
                self.load_preview();
            }
        }
    }

    /// Open the name field, pre-filled with a free name for `intent`.
    fn begin_naming(&mut self, intent: NamingIntent) {
        let suggestion = match &intent {
            NamingIntent::New => {
                xai_grok_agent::suggest_preset_name(self.source.preset_name().unwrap_or("preset"))
            }
            NamingIntent::Rename(from) => from.clone(),
            NamingIntent::Duplicate(from) => xai_grok_agent::suggest_preset_name(from),
        };
        let mut editor = LineEditor::default();
        editor.set_text(&suggestion);
        self.mode = PromptsModalMode::NamingPreset {
            editor,
            error: None,
            intent,
        };
        self.status = None;
    }

    fn begin_edit(&mut self, id: PromptId) {
        let body = xai_grok_agent::resolve_body(id);
        let mut textarea = TextArea::new();
        textarea.set_text(&body);
        self.mode = PromptsModalMode::Editing {
            id,
            textarea,
            textarea_state: TextAreaState::default(),
            dirty: false,
            original: body,
        };
        self.status = None;
    }

    fn load_preview(&mut self) {
        self.preview_scroll = 0;
        match self.tab {
            PromptsTab::Catalog => {
                self.preview_markdown =
                    self.selected_entry().filter(|e| !e.is_header).and_then(|e| {
                        let id = e.id?;
                        let ep = xai_grok_agent::effective_prompt(id).ok()?;
                        let mut md = String::new();
                        md.push_str(&format!("# {}\n\n", id.title()));
                        md.push_str(&format!("{}\n\n", id.description()));
                        if ep.is_override {
                            md.push_str("**Status:** overridden (local)");
                            if let Some(ref p) = ep.override_path {
                                md.push_str(&format!("  \n**Path:** `{}`", p.display()));
                            }
                        } else {
                            md.push_str(
                                "**Status:** default (built-in — updates with Grok Build)",
                            );
                        }
                        md.push_str(&format!(
                            "  \n**Session source:** `{}`",
                            self.source.label()
                        ));
                        if id.is_minijinja_template() {
                            md.push_str(
                                "\n\n> Template uses MiniJinja `${{ }}` / `${% %}` — keep syntax intact.",
                            );
                        }
                        md.push_str(
                            "\n\n*Enter / e* edit in TUI · *E* external editor · *r* reset · *n* save as preset\n\n---\n\n```\n",
                        );
                        md.push_str(&ep.body);
                        if !ep.body.ends_with('\n') {
                            md.push('\n');
                        }
                        md.push_str("```\n");
                        Some(MarkdownContent::new(md))
                    });
            }
            PromptsTab::Presets => {
                let selected = self.selected_preset().cloned();
                let source_label = self.source.label();
                self.preview_markdown = if let Some(p) = selected {
                    let mut md = format!("# Preset `{}`\n\n", p.name);
                    if let Some(ref description) = p.description {
                        md.push_str(&format!("{description}\n\n"));
                    }
                    md.push_str(&format!(
                        "**Overrides:** {} prompt(s)\n\n",
                        p.override_count
                    ));
                    if let Some(ref updated) = p.updated {
                        md.push_str(&format!("**Updated:** {updated}\n\n"));
                    }
                    if p.is_active {
                        md.push_str("**Active** — this session resolves through this preset.\n\n");
                    }
                    md.push_str(&format!(
                        "This session currently reads from: `{source_label}`.\n\n"
                    ));
                    md.push_str(
                        "Presets live in `$GROK_HOME/prompt-presets/<name>/`, outside any git repo.\n\
                         Each session binds to its own preset, so a parallel session is never disturbed.\n\
                         Prompts you never edited keep coming from the binary, so updates still deliver them.\n\n\
                         *Enter* use here · *n* new · *u* update from session · *R* rename · *y* duplicate · *d* delete · *c* defaults only\n",
                    );
                    Some(MarkdownContent::new(md))
                } else {
                    Some(MarkdownContent::new(format!(
                        "# Presets\n\nNo preset matches.\n\n\
                         Press **n** to save this session's prompts as a named preset.\n\n\
                         This session currently reads from: `{source_label}`.\n\n\
                         Personal prompts stay under `$GROK_HOME` and are never part of the source tree or GitHub rebuilds.\n",
                    )))
                };
            }
        }
    }
}

fn build_catalog_entries() -> Vec<PromptListEntry> {
    let defs = list_prompts();
    let mut out = Vec::new();
    for cat in PromptCategory::ALL {
        let in_cat: Vec<_> = defs.iter().filter(|d| d.category == *cat).collect();
        if in_cat.is_empty() {
            continue;
        }
        out.push(PromptListEntry {
            id: None,
            label: cat.label().to_string(),
            meta_display: String::new(),
            is_header: true,
            is_override: false,
        });
        for d in in_cat {
            let is_override = xai_grok_agent::has_override(d.id);
            out.push(PromptListEntry {
                id: Some(d.id),
                label: d.title.to_string(),
                meta_display: if is_override {
                    "modified".to_string()
                } else {
                    "default".to_string()
                },
                is_header: false,
                is_override,
            });
        }
    }
    out
}

fn compute_filtered_presets(presets: &[PromptPresetInfo], query: &str) -> Vec<usize> {
    if query.is_empty() {
        return (0..presets.len()).collect();
    }
    let needle = query.to_lowercase();
    presets
        .iter()
        .enumerate()
        .filter(|(_, p)| {
            p.name.to_lowercase().contains(&needle)
                || p.description
                    .as_deref()
                    .is_some_and(|d| d.to_lowercase().contains(&needle))
        })
        .map(|(i, _)| i)
        .collect()
}

fn compute_filtered(entries: &[PromptListEntry], query: &str) -> Vec<usize> {
    if query.is_empty() {
        return (0..entries.len()).collect();
    }
    let needle = query.to_lowercase();
    let mut result = Vec::new();
    let mut pending_header: Option<usize> = None;
    for (i, entry) in entries.iter().enumerate() {
        if entry.is_header {
            pending_header = Some(i);
        } else if entry.label.to_lowercase().contains(&needle)
            || entry
                .id
                .map(|id| {
                    id.as_str().contains(&needle)
                        || id.description().to_lowercase().contains(&needle)
                })
                .unwrap_or(false)
        {
            if let Some(h) = pending_header.take() {
                result.push(h);
            }
            result.push(i);
        }
    }
    result
}

fn sat_u16(v: usize) -> u16 {
    v.min(u16::MAX as usize) as u16
}

fn truncate_to_width(s: &str, max: usize) -> String {
    if s.width() <= max {
        return s.to_string();
    }
    let mut out = String::new();
    let mut w = 0;
    for ch in s.chars() {
        let cw = unicode_width::UnicodeWidthChar::width(ch).unwrap_or(0);
        if w + cw > max {
            break;
        }
        out.push(ch);
        w += cw;
    }
    out
}

pub fn render_prompts_modal(
    buf: &mut Buffer,
    full_area: Rect,
    state: &mut PromptsModalState,
    compact: bool,
) {
    let theme = Theme::current();
    let tab_labels: Vec<&str> = PromptsTab::ALL.iter().map(|t| t.label()).collect();
    state.window.active_tab = PromptsTab::ALL
        .iter()
        .position(|t| *t == state.tab)
        .unwrap_or(0);

    let title = if let Some(ref p) = state.active_preset {
        // ModalWindow title is &'static — keep static title; show preset in preview.
        let _ = p;
        MODAL_TITLE
    } else {
        MODAL_TITLE
    };

    let shortcuts = build_shortcuts(state);
    let modal_config = ModalWindowConfig {
        title,
        tabs: Some(&tab_labels),
        shortcuts: &shortcuts,
        sizing: if state.fullscreen {
            ModalSizing {
                width_pct: 1.0,
                max_width: u16::MAX,
                min_width: 44,
                v_margin: 0,
                h_pad: 2,
                v_pad: 0,
                footer_lines: 2,
            }
        } else {
            ModalSizing {
                width_pct: 0.80,
                max_width: 150,
                min_width: 44,
                v_margin: 2,
                h_pad: 2,
                v_pad: 1,
                footer_lines: 2,
            }
        }
        .with_compact(compact),
        fold_info: None,
    };

    let Some(ModalContentArea {
        content: content_area,
        ..
    }) =
        modal_window::render_modal_window(buf, full_area, &mut state.window, &modal_config, &theme)
    else {
        return;
    };

    if content_area.height < 2 || content_area.width < 10 {
        state.cursor_pos = None;
        state.edit_area = None;
        return;
    }

    // Status strip
    // Always occupied: without a status the strip names the session's prompt
    // source, so what the session actually runs on is never a guess.
    let mut body = content_area;
    {
        let (text, fg) = match state.status {
            Some(ref status) => (status.clone(), theme.accent_success),
            None => (
                format!("Source: {}", state.source.label()),
                theme.text_secondary,
            ),
        };
        let style = Style::default().fg(fg).bg(theme.bg_base);
        buf.set_span(
            body.x,
            body.y,
            &Span::styled(truncate_to_width(&text, body.width as usize), style),
            body.width,
        );
        body.y += 1;
        body.height = body.height.saturating_sub(1);
    }

    if matches!(state.mode, PromptsModalMode::Editing { .. }) {
        render_editor(buf, body, state, &theme);
        return;
    }
    if matches!(state.mode, PromptsModalMode::NamingPreset { .. }) {
        render_naming(buf, body, state, &theme);
        return;
    }

    // Browse / filter: no hardware caret on the catalog.
    state.cursor_pos = None;
    state.edit_area = None;

    let show_preview = body.width >= SPLIT_MIN_WIDTH;
    let list_width = if show_preview {
        (body.width as f64 * LIST_WIDTH_RATIO) as u16
    } else {
        body.width
    };

    let list_area = Rect {
        x: body.x,
        y: body.y,
        width: list_width,
        height: body.height,
    };
    state.list_area = list_area;
    render_list(buf, list_area, state, &theme);

    if show_preview {
        let preview_x = body.x + list_width + 1;
        let preview_width = body.width.saturating_sub(list_width + 1);
        if preview_width > 2 {
            let sep_x = body.x + list_width;
            let sep_style = Style::default().fg(theme.gray_dim);
            for y in body.y..body.y + body.height {
                if let Some(cell) = buf.cell_mut((sep_x, y)) {
                    cell.set_symbol("\u{2502}");
                    cell.set_style(sep_style);
                }
            }
            let preview_area = Rect {
                x: preview_x,
                y: body.y,
                width: preview_width,
                height: body.height,
            };
            state.preview_area = preview_area;
            render_preview(buf, preview_area, state, &theme);
        }
    } else {
        state.preview_area = Rect::default();
        state.preview_scrollbar_area = None;
    }
}

fn render_editor(buf: &mut Buffer, area: Rect, state: &mut PromptsModalState, theme: &Theme) {
    let PromptsModalMode::Editing {
        id,
        textarea,
        textarea_state,
        dirty,
        ..
    } = &mut state.mode
    else {
        return;
    };
    let header = format!(
        "Editing {} {} — Ctrl+S save · Esc cancel · Ctrl+E $EDITOR · click to place caret",
        id.title(),
        if *dirty { "(modified)" } else { "" }
    );
    buf.set_span(
        area.x,
        area.y,
        &Span::styled(
            truncate_to_width(&header, area.width as usize),
            Style::default()
                .fg(theme.accent_user)
                .bg(theme.bg_base)
                .add_modifier(Modifier::BOLD),
        ),
        area.width,
    );
    if area.height < 2 {
        state.edit_area = None;
        state.cursor_pos = None;
        return;
    }
    // One-row inset so the field reads as a distinct editor surface.
    let edit_area = Rect {
        x: area.x,
        y: area.y + 1,
        width: area.width,
        height: area.height.saturating_sub(1),
    };
    // Panel bg slightly distinct from modal chrome so the typing surface is obvious.
    let field_bg = theme.bg_dark;
    buf.set_style(edit_area, Style::default().bg(field_bg));
    // Paint default text colour into empty cells so Style::default() text from
    // TextArea (which inherits terminal default) still has a coherent field.
    for y in edit_area.y..edit_area.y + edit_area.height {
        for x in edit_area.x..edit_area.x + edit_area.width {
            if let Some(cell) = buf.cell_mut((x, y)) {
                cell.set_bg(field_bg);
                cell.set_fg(theme.text_primary);
            }
        }
    }
    textarea.scrollbar_track_style = Style::default().bg(field_bg);
    textarea.scrollbar_thumb_style = Style::default().fg(theme.gray_bright).bg(field_bg);
    textarea.selection_style = Style::default()
        .bg(theme.bg_visual)
        .fg(theme.text_primary);
    StatefulWidgetRef::render_ref(&&*textarea, edit_area, buf, textarea_state);
    // Re-apply fg on painted text cells that still use Reset fg.
    for y in edit_area.y..edit_area.y + edit_area.height {
        for x in edit_area.x..edit_area.x + edit_area.width {
            if let Some(cell) = buf.cell_mut((x, y)) {
                if cell.bg == ratatui::style::Color::Reset {
                    cell.set_bg(field_bg);
                }
                if cell.fg == ratatui::style::Color::Reset {
                    cell.set_fg(theme.text_primary);
                }
            }
        }
    }
    state.edit_area = Some(edit_area);
    // Hardware terminal caret only (blinking |) — same as the main prompt
    // widget. A soft reverse cell would sit on top and never blink.
    state.cursor_pos = textarea.cursor_pos_with_state(edit_area, *textarea_state);
}

fn render_naming(buf: &mut Buffer, area: Rect, state: &mut PromptsModalState, theme: &Theme) {
    let PromptsModalMode::NamingPreset {
        editor,
        error,
        intent,
    } = &state.mode
    else {
        return;
    };
    let title = intent.title();
    buf.set_span(
        area.x,
        area.y,
        &Span::styled(
            title.as_str(),
            Style::default()
                .fg(theme.accent_user)
                .bg(theme.bg_base)
                .add_modifier(Modifier::BOLD),
        ),
        area.width,
    );
    let y = area.y + 2;
    let label = "Name: ";
    buf.set_span(
        area.x,
        y,
        &Span::styled(label, Style::default().fg(theme.text_secondary).bg(theme.bg_base)),
        area.width,
    );
    let field_x = area.x + label.len() as u16;
    let field_w = area.width.saturating_sub(label.len() as u16).max(1);
    let field_bg = theme.bg_dark;
    // Draw a clear input field band.
    buf.set_style(
        Rect {
            x: field_x,
            y,
            width: field_w,
            height: 1,
        },
        Style::default().bg(field_bg),
    );
    let vp = editor.viewport(field_w as usize);
    let text = &editor.text()[vp.visible_byte_range.clone()];
    buf.set_span(
        field_x,
        y,
        &Span::styled(text, Style::default().fg(theme.text_primary).bg(field_bg)),
        field_w,
    );
    // Hardware caret only (blinking |) — match main prompt / line fields.
    let cx = (field_x + vp.cursor_display_column as u16).min(field_x + field_w.saturating_sub(1));
    state.cursor_pos = Some((cx, y));
    state.edit_area = Some(Rect {
        x: field_x,
        y,
        width: field_w,
        height: 1,
    });
    if let Some(err) = error {
        buf.set_span(
            area.x,
            y + 2,
            &Span::styled(
                truncate_to_width(err, area.width as usize),
                Style::default().fg(theme.accent_error).bg(theme.bg_base),
            ),
            area.width,
        );
    }
    buf.set_span(
        area.x,
        area.y + area.height.saturating_sub(1),
        &Span::styled(
            "a-z 0-9 _ - . · Enter save · Esc cancel · click to place caret",
            Style::default().fg(theme.gray_dim).bg(theme.bg_base),
        ),
        area.width,
    );
}

fn build_shortcuts(state: &PromptsModalState) -> Vec<Shortcut<'static>> {
    match &state.mode {
        PromptsModalMode::Editing { .. } => vec![
            Shortcut {
                label: "Ctrl+S save",
                clickable: false,
                id: 0,
            },
            Shortcut {
                label: "Esc cancel",
                clickable: false,
                id: 0,
            },
            Shortcut {
                label: "Ctrl+E $EDITOR",
                clickable: false,
                id: 0,
            },
        ],
        PromptsModalMode::NamingPreset { .. } => vec![
            Shortcut {
                label: "Enter save",
                clickable: false,
                id: 0,
            },
            Shortcut {
                label: "Esc cancel",
                clickable: false,
                id: 0,
            },
        ],
        PromptsModalMode::ConfirmingReset { .. } => vec![Shortcut {
            label: "r confirm reset",
            clickable: false,
            id: 0,
        }],
        PromptsModalMode::ConfirmingDeletePreset { .. } => vec![Shortcut {
            label: "d confirm delete",
            clickable: false,
            id: 0,
        }],
        PromptsModalMode::ConfirmingOverwritePreset { .. } => vec![
            Shortcut {
                label: "o / Enter replace",
                clickable: false,
                id: 0,
            },
            Shortcut {
                label: "Esc keep existing",
                clickable: false,
                id: 0,
            },
        ],
        PromptsModalMode::ConfirmingDefaults => vec![
            Shortcut {
                label: "c confirm defaults",
                clickable: false,
                id: 0,
            },
            Shortcut {
                label: "Esc cancel",
                clickable: false,
                id: 0,
            },
        ],
        PromptsModalMode::FilterFocused => vec![
            Shortcut {
                label: "type to filter",
                clickable: false,
                id: 0,
            },
            Shortcut {
                label: "Esc done",
                clickable: false,
                id: 0,
            },
        ],
        PromptsModalMode::Browse => match state.tab {
            PromptsTab::Catalog => vec![
                Shortcut {
                    label: "\u{2191}/\u{2193} nav",
                    clickable: false,
                    id: 0,
                },
                Shortcut {
                    label: "Enter edit",
                    clickable: false,
                    id: 0,
                },
                Shortcut {
                    label: "r reset",
                    clickable: false,
                    id: 0,
                },
                Shortcut {
                    label: "n save preset",
                    clickable: false,
                    id: 0,
                },
                Shortcut {
                    label: "Tab presets",
                    clickable: false,
                    id: 0,
                },
                Shortcut {
                    label: "Esc close",
                    clickable: false,
                    id: 0,
                },
            ],
            PromptsTab::Presets => vec![
                Shortcut {
                    label: "Enter use here",
                    clickable: false,
                    id: 0,
                },
                Shortcut {
                    label: "n new",
                    clickable: false,
                    id: 0,
                },
                Shortcut {
                    label: "u update",
                    clickable: false,
                    id: 0,
                },
                Shortcut {
                    label: "R rename",
                    clickable: false,
                    id: 0,
                },
                Shortcut {
                    label: "y duplicate",
                    clickable: false,
                    id: 0,
                },
                Shortcut {
                    label: "d delete",
                    clickable: false,
                    id: 0,
                },
                Shortcut {
                    label: "c defaults",
                    clickable: false,
                    id: 0,
                },
                Shortcut {
                    label: "/ filter",
                    clickable: false,
                    id: 0,
                },
            ],
        },
    }
}

fn render_list(buf: &mut Buffer, area: Rect, state: &mut PromptsModalState, theme: &Theme) {
    let search_y = area.y;
    let filter_focused = matches!(state.mode, PromptsModalMode::FilterFocused);
    // Both tabs filter: a long preset list needs `/` as much as the catalog.
    {
        let viewport = state.query.viewport(area.width as usize);
        if state.query().is_empty() {
            let placeholder = if filter_focused {
                "type to filter..."
            } else {
                "/ to filter..."
            };
            buf.set_span(
                area.x,
                search_y,
                &Span::styled(
                    placeholder,
                    Style::default().fg(theme.gray_dim).bg(theme.bg_base),
                ),
                area.width,
            );
        } else {
            let leading;
            let visible = if filter_focused {
                &state.query()[viewport.visible_byte_range.clone()]
            } else {
                leading =
                    crate::render::line_utils::truncate_str(state.query(), area.width as usize);
                &leading
            };
            buf.set_span(
                area.x,
                search_y,
                &Span::styled(
                    visible,
                    Style::default().fg(theme.text_primary).bg(theme.bg_base),
                ),
                area.width,
            );
        }
        if filter_focused {
            let cursor_x = area.x + viewport.cursor_display_column as u16;
            if cursor_x < area.x + area.width
                && let Some(cell) = buf.cell_mut((cursor_x, search_y))
            {
                cell.set_style(Style::default().fg(theme.bg_base).bg(theme.text_primary));
            }
        }
    }

    let entries_start_y = search_y + 1;
    let available_height = area.height.saturating_sub(1) as usize;
    if state.selected < state.scroll_offset {
        state.scroll_offset = state.selected;
    }
    if state.selected >= state.scroll_offset + available_height {
        state.scroll_offset = state
            .selected
            .saturating_sub(available_height.saturating_sub(1));
    }

    let total = state.filtered_cache.len();
    let total_entries = sat_u16(total);
    let sb_area = if total_entries > available_height as u16 && area.width > 4 {
        Some(Rect {
            x: area.x + area.width - 1,
            y: entries_start_y,
            width: 1,
            height: available_height as u16,
        })
    } else {
        None
    };
    state.list_scrollbar_area = sb_area;
    let content_width = if sb_area.is_some() {
        area.width.saturating_sub(2)
    } else {
        area.width
    };

    match state.tab {
        PromptsTab::Catalog => {
            let filtered = &state.filtered_cache;
            let end = filtered.len().min(state.scroll_offset + available_height);
            let visible = &filtered[state.scroll_offset..end];
            for (row, &orig_idx) in visible.iter().enumerate() {
                let y = entries_start_y + row as u16;
                if y >= area.y + area.height {
                    break;
                }
                let entry = &state.entries[orig_idx];
                let filt_idx = state.scroll_offset + row;
                let is_selected = filt_idx == state.selected;
                if entry.is_header {
                    let header_style = Style::default()
                        .fg(theme.accent_user)
                        .bg(theme.bg_base)
                        .add_modifier(Modifier::BOLD);
                    buf.set_line(
                        area.x,
                        y,
                        &Line::from(Span::styled(&entry.label, header_style)),
                        content_width,
                    );
                } else {
                    paint_row(
                        buf,
                        area.x,
                        y,
                        content_width,
                        &entry.label,
                        entry.is_override,
                        &entry.meta_display,
                        is_selected,
                        matches!(
                            state.mode,
                            PromptsModalMode::ConfirmingReset { idx } if idx == filt_idx
                        ),
                        theme,
                    );
                }
            }
        }
        PromptsTab::Presets => {
            let filtered = &state.filtered_cache;
            let end = filtered.len().min(state.scroll_offset + available_height);
            for (row, &orig_idx) in filtered[state.scroll_offset..end].iter().enumerate() {
                let Some(p) = state.presets.get(orig_idx) else {
                    continue;
                };
                let y = entries_start_y + row as u16;
                let filt_idx = state.scroll_offset + row;
                let is_selected = filt_idx == state.selected;
                let badge = if p.is_active { " \u{25cf}" } else { "" };
                let label = format!("{}{badge}", p.name);
                let meta = format!("{} ov", p.override_count);
                paint_row(
                    buf,
                    area.x,
                    y,
                    content_width,
                    &label,
                    p.is_active,
                    &meta,
                    is_selected,
                    matches!(
                        &state.mode,
                        PromptsModalMode::ConfirmingDeletePreset { name } if name == &p.name
                    ),
                    theme,
                );
            }
        }
    }

    render_scrollbar(
        buf,
        sb_area,
        total_entries,
        sat_u16(available_height),
        sat_u16(state.scroll_offset),
        false,
    );
}

fn paint_row(
    buf: &mut Buffer,
    x: u16,
    y: u16,
    content_width: u16,
    label: &str,
    emphasize: bool,
    meta: &str,
    is_selected: bool,
    confirming: bool,
    theme: &Theme,
) {
    let bg = if is_selected {
        theme.bg_visual
    } else {
        theme.bg_base
    };
    buf.set_style(
        Rect {
            x,
            y,
            width: content_width,
            height: 1,
        },
        Style::default().bg(bg),
    );
    let label_style = Style::default().fg(theme.text_primary).bg(bg);
    let max_label_w = content_width.saturating_sub(2) as usize;
    let truncated: Cow<str> = if label.width() > max_label_w {
        format!("{}...", truncate_to_width(label, max_label_w.saturating_sub(3))).into()
    } else {
        Cow::Borrowed(label)
    };
    buf.set_span(
        x + 1,
        y,
        &Span::styled(truncated.as_ref(), label_style),
        content_width.saturating_sub(1),
    );
    if confirming {
        let hint = " [confirm]";
        let hint_w = hint.len() as u16;
        let hint_x = (x + content_width).saturating_sub(hint_w + 1);
        buf.set_span(
            hint_x,
            y,
            &Span::styled(hint, Style::default().fg(theme.accent_error).bg(bg)),
            hint_w,
        );
    } else {
        let meta_style = Style::default()
            .fg(if emphasize { theme.warning } else { theme.gray })
            .bg(bg);
        let meta_w = meta.width() as u16;
        let meta_x = (x + content_width).saturating_sub(meta_w + 1);
        if meta_x > x + 1 {
            buf.set_span(meta_x, y, &Span::styled(meta, meta_style), meta_w);
        }
    }
}

fn render_preview(buf: &mut Buffer, area: Rect, state: &mut PromptsModalState, theme: &Theme) {
    if state.preview_markdown.is_none() {
        state.preview_total_lines = 0;
        state.preview_scrollbar_area = None;
        return;
    }
    let full_width = area.width as usize;
    if full_width == 0 {
        return;
    }
    buf.set_style(area, Style::default().bg(theme.bg_base));
    let total_at_full = state
        .preview_markdown
        .as_ref()
        .unwrap()
        .with_wrapped_lines(full_width, |w| w.lines.len());
    let visible = area.height as usize;
    let (content_width, sb_area) = if total_at_full > visible && area.width > 4 {
        (
            full_width.saturating_sub(2),
            Some(Rect {
                x: area.x + area.width - 1,
                y: area.y,
                width: 1,
                height: area.height,
            }),
        )
    } else {
        (full_width, None)
    };
    let total = if sb_area.is_some() && content_width != full_width {
        state
            .preview_markdown
            .as_ref()
            .unwrap()
            .with_wrapped_lines(content_width, |w| w.lines.len())
    } else {
        total_at_full
    };
    state.preview_total_lines = total;
    state.preview_scrollbar_area = sb_area;
    state.preview_scroll = state.preview_scroll.min(total.saturating_sub(visible));
    let scroll = state.preview_scroll;
    state
        .preview_markdown
        .as_ref()
        .unwrap()
        .with_wrapped_lines(content_width, |wrapped| {
            for (row, line_idx) in (scroll..total.min(scroll + visible)).enumerate() {
                let y = area.y + row as u16;
                buf.set_line_safe(area.x, y, &wrapped.lines[line_idx], content_width as u16);
            }
        });
    render_scrollbar(
        buf,
        sb_area,
        sat_u16(total),
        sat_u16(visible),
        sat_u16(scroll),
        false,
    );
}

pub fn handle_prompts_key(state: &mut PromptsModalState, key: &KeyEvent) -> InputOutcome {
    if key.kind == KeyEventKind::Release {
        return InputOutcome::Unchanged;
    }

    // Modal tab chrome may already handle Tab — also accept here.
    if matches!(key.code, KeyCode::Tab | KeyCode::BackTab)
        && matches!(
            state.mode,
            PromptsModalMode::Browse | PromptsModalMode::FilterFocused
        )
    {
        let next = match state.tab {
            PromptsTab::Catalog => PromptsTab::Presets,
            PromptsTab::Presets => PromptsTab::Catalog,
        };
        state.switch_tab(next);
        return InputOutcome::Changed;
    }

    match &mut state.mode {
        PromptsModalMode::Editing {
            id,
            textarea,
            dirty,
            original,
            ..
        } => {
            // Ctrl+S save
            if key.code == KeyCode::Char('s') && key.modifiers.contains(KeyModifiers::CONTROL) {
                let body = textarea.text().to_string();
                let id = *id;
                match xai_grok_agent::save_override(id, &body) {
                    Ok(_) => {
                        state.status = Some(format!("Saved {}", id.as_str()));
                        state.mode = PromptsModalMode::Browse;
                        state.refresh();
                    }
                    Err(e) => {
                        state.status = Some(format!("Save failed: {e}"));
                    }
                }
                return InputOutcome::Changed;
            }
            // Ctrl+E external
            if key.code == KeyCode::Char('e') && key.modifiers.contains(KeyModifiers::CONTROL) {
                let id = *id;
                // Persist current buffer first if dirty.
                if *dirty {
                    let _ = xai_grok_agent::save_override(id, textarea.text());
                }
                match xai_grok_agent::materialize_for_edit(id) {
                    Ok(path) => {
                        state.mode = PromptsModalMode::Browse;
                        return InputOutcome::Action(Action::SuspendForEditor {
                            path,
                            refresh_agents_modal: None,
                            refresh_prompts_modal: true,
                        });
                    }
                    Err(e) => {
                        state.status = Some(format!("Cannot open editor: {e}"));
                        return InputOutcome::Changed;
                    }
                }
            }
            if key.code == KeyCode::Esc {
                state.mode = PromptsModalMode::Browse;
                state.status = Some("Edit cancelled".into());
                state.load_preview();
                return InputOutcome::Changed;
            }
            textarea.input(*key);
            *dirty = textarea.text() != original.as_str();
            InputOutcome::Changed
        }
        PromptsModalMode::NamingPreset {
            editor,
            error,
            intent,
        } => {
            if key.code == KeyCode::Esc {
                state.mode = PromptsModalMode::Browse;
                return InputOutcome::Changed;
            }
            if key.code == KeyCode::Enter {
                let name = editor.text().trim().to_string();
                let intent = intent.clone();
                let outcome = match &intent {
                    NamingIntent::New => {
                        xai_grok_agent::save_preset(&name, PresetWriteMode::Create)
                    }
                    NamingIntent::Rename(from) => xai_grok_agent::rename_preset(from, &name),
                    NamingIntent::Duplicate(from) => xai_grok_agent::duplicate_preset(from, &name),
                };
                match outcome {
                    Ok(()) => {
                        state.status = Some(match &intent {
                            NamingIntent::New => format!("Preset `{name}` saved"),
                            NamingIntent::Rename(from) => {
                                format!("Preset `{from}` renamed to `{name}`")
                            }
                            NamingIntent::Duplicate(from) => {
                                format!("Preset `{from}` duplicated as `{name}`")
                            }
                        });
                        state.mode = PromptsModalMode::Browse;
                        state.tab = PromptsTab::Presets;
                        state.refresh();
                    }
                    // A fresh save onto a name that exists asks first — the old
                    // behaviour silently replaced the preset.
                    Err(xai_grok_agent::PromptCatalogError::PresetExists(_))
                        if intent == NamingIntent::New =>
                    {
                        state.mode = PromptsModalMode::ConfirmingOverwritePreset { name };
                    }
                    Err(e) => {
                        *error = Some(e.to_string());
                    }
                }
                return InputOutcome::Changed;
            }
            let _ = editor.handle_key(key);
            *error = None;
            InputOutcome::Changed
        }
        PromptsModalMode::ConfirmingReset { idx } => {
            let idx = *idx;
            if key.code == KeyCode::Char('r') {
                if let Some(&orig) = state.filtered_cache.get(idx)
                    && let Some(id) = state.entries[orig].id
                {
                    match xai_grok_agent::reset_override(id) {
                        Ok(()) => state.status = Some(format!("Reset {}", id.as_str())),
                        Err(e) => state.status = Some(format!("Reset failed: {e}")),
                    }
                }
                state.mode = PromptsModalMode::Browse;
                state.refresh();
                return InputOutcome::Changed;
            }
            state.mode = PromptsModalMode::Browse;
            InputOutcome::Changed
        }
        PromptsModalMode::ConfirmingDeletePreset { name } => {
            let name = name.clone();
            if key.code == KeyCode::Char('d') {
                match xai_grok_agent::delete_preset(&name) {
                    Ok(()) => state.status = Some(format!("Deleted preset `{name}`")),
                    Err(e) => state.status = Some(format!("Delete failed: {e}")),
                }
                state.mode = PromptsModalMode::Browse;
                state.refresh();
                return InputOutcome::Changed;
            }
            state.mode = PromptsModalMode::Browse;
            InputOutcome::Changed
        }
        PromptsModalMode::ConfirmingOverwritePreset { name } => {
            let name = name.clone();
            if matches!(key.code, KeyCode::Char('o') | KeyCode::Enter) {
                match xai_grok_agent::save_preset(&name, PresetWriteMode::Overwrite) {
                    Ok(()) => {
                        state.status = Some(format!("Preset `{name}` replaced"));
                        state.tab = PromptsTab::Presets;
                    }
                    Err(e) => state.status = Some(format!("Save failed: {e}")),
                }
                state.mode = PromptsModalMode::Browse;
                state.refresh();
                return InputOutcome::Changed;
            }
            state.status = Some("Kept the existing preset".into());
            state.mode = PromptsModalMode::Browse;
            InputOutcome::Changed
        }
        PromptsModalMode::ConfirmingDefaults => {
            if key.code == KeyCode::Char('c') {
                match xai_grok_agent::use_defaults_only() {
                    Ok(()) => {
                        state.status =
                            Some("This session now runs on built-in defaults".into())
                    }
                    Err(e) => state.status = Some(format!("Failed: {e}")),
                }
                state.mode = PromptsModalMode::Browse;
                state.refresh();
                return InputOutcome::Changed;
            }
            state.mode = PromptsModalMode::Browse;
            InputOutcome::Changed
        }
        PromptsModalMode::FilterFocused => handle_filter_focused(state, key),
        PromptsModalMode::Browse => handle_browse(state, key),
    }
}

pub fn handle_prompts_paste(state: &mut PromptsModalState, text: &str) -> InputOutcome {
    match &mut state.mode {
        PromptsModalMode::FilterFocused => {
            let outcome = state.query.insert_paste(text);
            finish_filter_edit(state, outcome)
        }
        PromptsModalMode::Editing { textarea, dirty, original, .. } => {
            textarea.insert_str(text);
            *dirty = textarea.text() != original.as_str();
            InputOutcome::Changed
        }
        PromptsModalMode::NamingPreset { editor, error, .. } => {
            let _ = editor.insert_paste(text);
            *error = None;
            InputOutcome::Changed
        }
        _ => InputOutcome::Unchanged,
    }
}

fn handle_filter_focused(state: &mut PromptsModalState, key: &KeyEvent) -> InputOutcome {
    match key.code {
        KeyCode::Esc => {
            state.mode = PromptsModalMode::Browse;
            InputOutcome::Changed
        }
        KeyCode::Down => {
            state.select_next();
            InputOutcome::Changed
        }
        KeyCode::Up => {
            state.select_prev();
            InputOutcome::Changed
        }
        _ => {
            let outcome = state.query.handle_key(key);
            finish_filter_edit(state, outcome)
        }
    }
}

fn finish_filter_edit(state: &mut PromptsModalState, outcome: LineEditOutcome) -> InputOutcome {
    match outcome {
        LineEditOutcome::TextChanged => {
            state.invalidate_filter();
            state.clamp_selected();
            InputOutcome::Changed
        }
        LineEditOutcome::CursorChanged | LineEditOutcome::HandledNoChange => InputOutcome::Changed,
        LineEditOutcome::Unhandled => InputOutcome::Unchanged,
    }
}

fn handle_browse(state: &mut PromptsModalState, key: &KeyEvent) -> InputOutcome {
    if key.code == KeyCode::Char('f') && key.modifiers.contains(KeyModifiers::CONTROL) {
        state.fullscreen = !state.fullscreen;
        return InputOutcome::Changed;
    }

    // Shared navigation
    match key.code {
        KeyCode::Down | KeyCode::Char('j') if key.modifiers.is_empty() => {
            state.select_next();
            return InputOutcome::Changed;
        }
        KeyCode::Up | KeyCode::Char('k') if key.modifiers.is_empty() => {
            state.select_prev();
            return InputOutcome::Changed;
        }
        KeyCode::Char('f') if key.modifiers.is_empty() => {
            state.fullscreen = !state.fullscreen;
            return InputOutcome::Changed;
        }
        _ => {}
    }

    match state.tab {
        PromptsTab::Catalog => handle_catalog_browse(state, key),
        PromptsTab::Presets => handle_presets_browse(state, key),
    }
}

fn handle_catalog_browse(state: &mut PromptsModalState, key: &KeyEvent) -> InputOutcome {
    match key.code {
        KeyCode::Char('/') if key.modifiers.is_empty() => {
            state.mode = PromptsModalMode::FilterFocused;
            InputOutcome::Changed
        }
        KeyCode::Enter | KeyCode::Char('e') if key.modifiers.is_empty() => {
            let Some(entry) = state.selected_entry() else {
                return InputOutcome::Unchanged;
            };
            if entry.is_header {
                return InputOutcome::Unchanged;
            }
            let Some(id) = entry.id else {
                return InputOutcome::Unchanged;
            };
            state.begin_edit(id);
            InputOutcome::Changed
        }
        KeyCode::Char('E') => {
            // External editor
            let Some(entry) = state.selected_entry() else {
                return InputOutcome::Unchanged;
            };
            if entry.is_header {
                return InputOutcome::Unchanged;
            }
            let Some(id) = entry.id else {
                return InputOutcome::Unchanged;
            };
            match xai_grok_agent::materialize_for_edit(id) {
                Ok(path) => InputOutcome::Action(Action::SuspendForEditor {
                    path,
                    refresh_agents_modal: None,
                    refresh_prompts_modal: true,
                }),
                Err(e) => {
                    state.status = Some(format!("Cannot open editor: {e}"));
                    InputOutcome::Changed
                }
            }
        }
        KeyCode::Char('r') if key.modifiers.is_empty() => {
            let Some(entry) = state.selected_entry() else {
                return InputOutcome::Unchanged;
            };
            if entry.is_header || !entry.is_override {
                return InputOutcome::Unchanged;
            }
            state.mode = PromptsModalMode::ConfirmingReset {
                idx: state.selected,
            };
            InputOutcome::Changed
        }
        KeyCode::Char('n') if key.modifiers.is_empty() => {
            state.begin_naming(NamingIntent::New);
            InputOutcome::Changed
        }
        _ => InputOutcome::Unchanged,
    }
}

fn handle_presets_browse(state: &mut PromptsModalState, key: &KeyEvent) -> InputOutcome {
    match key.code {
        KeyCode::Char('/') if key.modifiers.is_empty() => {
            state.mode = PromptsModalMode::FilterFocused;
            InputOutcome::Changed
        }
        KeyCode::Enter if key.modifiers.is_empty() => {
            let Some(name) = state.selected_preset_name() else {
                return InputOutcome::Unchanged;
            };
            match xai_grok_agent::apply_preset(&name) {
                Ok(()) => {
                    state.status = Some(format!("This session now uses `{name}`"));
                    state.refresh();
                }
                Err(e) => state.status = Some(format!("Apply failed: {e}")),
            }
            InputOutcome::Changed
        }
        KeyCode::Char('n') if key.modifiers.is_empty() => {
            state.begin_naming(NamingIntent::New);
            InputOutcome::Changed
        }
        KeyCode::Char('u') if key.modifiers.is_empty() => {
            // Explicit overwrite of the selected preset from this session's prompts.
            let Some(name) = state.selected_preset_name() else {
                return InputOutcome::Unchanged;
            };
            match xai_grok_agent::save_preset(&name, PresetWriteMode::Overwrite) {
                Ok(()) => {
                    state.status = Some(format!("Updated preset `{name}`"));
                    state.refresh();
                }
                Err(e) => state.status = Some(format!("Update failed: {e}")),
            }
            InputOutcome::Changed
        }
        KeyCode::Char('R') => {
            let Some(name) = state.selected_preset_name() else {
                return InputOutcome::Unchanged;
            };
            state.begin_naming(NamingIntent::Rename(name));
            InputOutcome::Changed
        }
        KeyCode::Char('y') if key.modifiers.is_empty() => {
            let Some(name) = state.selected_preset_name() else {
                return InputOutcome::Unchanged;
            };
            state.begin_naming(NamingIntent::Duplicate(name));
            InputOutcome::Changed
        }
        KeyCode::Char('d') if key.modifiers.is_empty() => {
            let Some(name) = state.selected_preset_name() else {
                return InputOutcome::Unchanged;
            };
            state.mode = PromptsModalMode::ConfirmingDeletePreset { name };
            InputOutcome::Changed
        }
        KeyCode::Char('c') if key.modifiers.is_empty() => {
            state.mode = PromptsModalMode::ConfirmingDefaults;
            InputOutcome::Changed
        }
        _ => InputOutcome::Unchanged,
    }
}

pub fn handle_prompts_mouse(state: &mut PromptsModalState, mouse: &MouseEvent) -> InputOutcome {
    // ── In-TUI editor: click/drag to place caret & select (same as prompt widget) ──
    if let PromptsModalMode::Editing {
        textarea,
        textarea_state,
        dirty,
        original,
        ..
    } = &mut state.mode
    {
        let Some(ta) = state.edit_area else {
            return InputOutcome::Unchanged;
        };
        let action = textarea.handle_mouse(*mouse, ta, *textarea_state);
        // Keep TextAreaState.scroll in sync with scroll_override after wheel.
        if matches!(action, xai_ratatui_textarea::MouseAction::Scrolled)
            && let Some(scroll) = textarea.scroll_override()
        {
            textarea_state.scroll = scroll;
        }
        *dirty = textarea.text() != original.as_str();
        return match action {
            xai_ratatui_textarea::MouseAction::Nothing => InputOutcome::Unchanged,
            _ => InputOutcome::Changed,
        };
    }

    // ── Naming field: click places caret in the single-line name editor ──
    if let PromptsModalMode::NamingPreset { editor, .. } = &mut state.mode {
        if matches!(mouse.kind, MouseEventKind::Down(MouseButton::Left))
            && let Some(field) = state.edit_area
            && mouse.column >= field.x
            && mouse.column < field.x + field.width
            && mouse.row == field.y
        {
            // Approximate byte cursor from display column.
            let col = (mouse.column - field.x) as usize;
            let text = editor.text();
            let mut display = 0usize;
            let mut byte = 0usize;
            for (i, ch) in text.char_indices() {
                let w = unicode_width::UnicodeWidthChar::width(ch).unwrap_or(0);
                if display + w > col {
                    byte = i;
                    break;
                }
                display += w;
                byte = i + ch.len_utf8();
            }
            let _ = editor.set_cursor_byte(byte);
            return InputOutcome::Changed;
        }
        return InputOutcome::Unchanged;
    }

    let kind = mouse.kind;
    let column = mouse.column;
    let row = mouse.row;
    let in_rect = |r: Rect| -> bool {
        r.width > 0
            && r.height > 0
            && column >= r.x
            && column < r.x + r.width
            && row >= r.y
            && row < r.y + r.height
    };
    let on_list = in_rect(state.list_area);
    let on_preview = in_rect(state.preview_area);
    let on_list_sb = state.list_scrollbar_area.is_some_and(&in_rect);
    let on_preview_sb = state.preview_scrollbar_area.is_some_and(&in_rect);

    match kind {
        MouseEventKind::Down(MouseButton::Left) | MouseEventKind::Drag(MouseButton::Left) => {
            if on_list_sb {
                let sb = state.list_scrollbar_area.unwrap();
                let total = match state.tab {
                    PromptsTab::Catalog => sat_u16(state.filtered_cache.len()),
                    PromptsTab::Presets => sat_u16(state.presets.len()),
                };
                let visible = state.list_area.height.saturating_sub(1);
                apply_scrollbar_jump(row, sb, total, visible, &mut state.scroll_offset);
                return InputOutcome::Changed;
            }
            if on_preview_sb {
                let sb = state.preview_scrollbar_area.unwrap();
                apply_scrollbar_jump(
                    row,
                    sb,
                    sat_u16(state.preview_total_lines),
                    state.preview_area.height,
                    &mut state.preview_scroll,
                );
                return InputOutcome::Changed;
            }
            if matches!(kind, MouseEventKind::Down(_)) && on_list {
                let entries_start_y = state.list_area.y + 1;
                if row >= entries_start_y {
                    let filt_idx = state.scroll_offset + (row - entries_start_y) as usize;
                    if state.select_at(filt_idx) {
                        return InputOutcome::Changed;
                    }
                }
            }
            InputOutcome::Unchanged
        }
        MouseEventKind::ScrollDown => {
            if on_list || on_list_sb {
                for _ in 0..3 {
                    if !state.advance_next() {
                        break;
                    }
                }
                state.load_preview();
                return InputOutcome::Changed;
            }
            if on_preview || on_preview_sb {
                let max = state
                    .preview_total_lines
                    .saturating_sub(state.preview_area.height as usize);
                state.preview_scroll = state.preview_scroll.saturating_add(3).min(max);
                return InputOutcome::Changed;
            }
            InputOutcome::Unchanged
        }
        MouseEventKind::ScrollUp => {
            if on_list || on_list_sb {
                for _ in 0..3 {
                    if !state.advance_prev() {
                        break;
                    }
                }
                state.load_preview();
                return InputOutcome::Changed;
            }
            if on_preview || on_preview_sb {
                state.preview_scroll = state.preview_scroll.saturating_sub(3);
                return InputOutcome::Changed;
            }
            InputOutcome::Unchanged
        }
        _ => InputOutcome::Unchanged,
    }
}

fn apply_scrollbar_jump(
    screen_row: u16,
    sb_area: Rect,
    total_lines: u16,
    viewport_lines: u16,
    offset: &mut usize,
) {
    let cell_index = screen_row.saturating_sub(sb_area.y);
    let result = scrollbar_click_to_offset(cell_index, sb_area.height, total_lines, viewport_lines);
    let max_scroll = (total_lines as usize).saturating_sub(viewport_lines as usize);
    match result {
        crate::render::scrollbar::ScrollbarClickResult::Top => *offset = 0,
        crate::render::scrollbar::ScrollbarClickResult::Bottom => *offset = max_scroll,
        crate::render::scrollbar::ScrollbarClickResult::Offset(o) => *offset = o.min(max_scroll),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn new_state_has_session_and_subagent_sections() {
        let state = PromptsModalState::new();
        assert!(
            state
                .entries
                .iter()
                .any(|e| e.is_header && e.label == "Session")
        );
        assert!(
            state
                .entries
                .iter()
                .any(|e| e.is_header && e.label == "Subagents")
        );
        assert!(
            state
                .entries
                .iter()
                .any(|e| e.id == Some(PromptId::BaseSystem))
        );
        assert!(state.selected_entry().is_some());
        assert!(state.preview_markdown.is_some());
        assert_eq!(state.tab, PromptsTab::Catalog);
    }

    #[test]
    fn filter_narrows_list() {
        let mut state = PromptsModalState::new();
        state.query.set_text("explore");
        state.invalidate_filter();
        state.clamp_selected();
        let ids: Vec<_> = state
            .filtered_cache
            .iter()
            .filter_map(|&i| state.entries[i].id)
            .collect();
        assert_eq!(ids, vec![PromptId::SubagentExplore]);
    }

    #[test]
    fn begin_edit_loads_body() {
        let mut state = PromptsModalState::new();
        state.begin_edit(PromptId::CompactSystem);
        match &state.mode {
            PromptsModalMode::Editing { id, textarea, dirty, .. } => {
                assert_eq!(*id, PromptId::CompactSystem);
                assert!(!dirty);
                assert!(!textarea.text().is_empty());
            }
            _ => panic!("expected Editing mode"),
        }
    }

    #[test]
    fn switch_tab_to_presets() {
        let mut state = PromptsModalState::new();
        state.switch_tab(PromptsTab::Presets);
        assert_eq!(state.tab, PromptsTab::Presets);
    }

    #[test]
    fn render_editor_exposes_hardware_caret() {
        let mut state = PromptsModalState::new();
        state.begin_edit(PromptId::CompactSystem);
        assert!(state.caret().is_none(), "caret only after paint");

        let area = Rect {
            x: 0,
            y: 0,
            width: 80,
            height: 24,
        };
        let mut buf = Buffer::empty(area);
        render_prompts_modal(&mut buf, area, &mut state, false);
        let caret = state.caret().expect("editing must expose caret after render");
        assert!(
            caret.0 < 80 && caret.1 < 24,
            "caret {caret:?} must be on-screen"
        );
        assert!(state.edit_area.is_some());
    }

    #[test]
    fn mouse_click_moves_editor_caret() {
        let mut state = PromptsModalState::new();
        state.begin_edit(PromptId::CompactSystem);
        // Seed a known body so click column maps to a non-zero cursor.
        if let PromptsModalMode::Editing {
            textarea, original, ..
        } = &mut state.mode
        {
            textarea.set_text("hello world");
            *original = "hello world".into();
        }
        let area = Rect {
            x: 0,
            y: 0,
            width: 80,
            height: 24,
        };
        let mut buf = Buffer::empty(area);
        render_prompts_modal(&mut buf, area, &mut state, false);
        let edit = state.edit_area.expect("edit_area after render");

        // Click roughly mid-word on the first line of the field.
        let mouse = MouseEvent {
            kind: MouseEventKind::Down(MouseButton::Left),
            column: edit.x + 5,
            row: edit.y,
            modifiers: KeyModifiers::empty(),
        };
        assert!(matches!(
            handle_prompts_mouse(&mut state, &mouse),
            InputOutcome::Changed
        ));
        if let PromptsModalMode::Editing { textarea, .. } = &state.mode {
            assert!(
                textarea.cursor() > 0,
                "click should place caret into the text, got {}",
                textarea.cursor()
            );
        } else {
            panic!("expected Editing");
        }
    }
}
