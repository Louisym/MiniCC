import { defineConfig } from 'vitepress'

export default defineConfig({
  title: 'Claude Reviews Claude',
  description: 'Claude Reviews Claude Code — 当 AI 阅读自己的源代码',
  base: '/claude-reviews-claude/',
  cleanUrls: true,
  lastUpdated: true,
  ignoreDeadLinks: true,

  head: [
    ['meta', { name: 'theme-color', content: '#D97757' }],
    ['meta', { name: 'og:type', content: 'website' }],
    ['meta', { name: 'og:title', content: 'Claude Code Deep Dive' }],
    ['meta', { name: 'og:description', content: '17 篇架构深度分析 — 由 Claude 亲笔解构 Claude Code' }],
    ['link', { rel: 'preconnect', href: 'https://fonts.googleapis.com' }],
    ['link', { rel: 'preconnect', href: 'https://fonts.gstatic.com', crossorigin: '' }],
    ['link', { href: 'https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap', rel: 'stylesheet' }],
  ],

  themeConfig: {
    logo: '/logo.svg',
    search: { provider: 'local' },
    socialLinks: [
      { icon: 'github', link: 'https://github.com/openedclaude/claude-reviews-claude' },
    ],
  },

  locales: {
    root: {
      label: 'English',
      lang: 'en',
      themeConfig: {
        nav: [
          { text: 'Overview', link: '/overview' },
          { text: 'Chapters', link: '/chapters/01-query-engine' },
          { text: 'GitHub', link: 'https://github.com/openedclaude/claude-reviews-claude' },
        ],
        sidebar: {
          '/': [
            {
              text: 'Getting Started',
              items: [
                { text: 'Overview', link: '/overview' },
              ],
            },
            {
              text: 'Architecture Deep Dive',
              items: [
                { text: '01 — Query Engine', link: '/chapters/01-query-engine' },
                { text: '02 — Tool System', link: '/chapters/02-tool-system' },
                { text: '03 — Coordinator', link: '/chapters/03-coordinator' },
                { text: '04 — Plugin System', link: '/chapters/04-plugin-system' },
                { text: '05 — Hook System', link: '/chapters/05-hook-system' },
                { text: '06 — Bash Engine', link: '/chapters/06-bash-engine' },
                { text: '07 — Permission Pipeline', link: '/chapters/07-permission-pipeline' },
                { text: '08 — Agent Swarms', link: '/chapters/08-agent-swarms' },
                { text: '09 — Session Persistence', link: '/chapters/09-session-persistence' },
                { text: '10 — Context Assembly', link: '/chapters/10-context-assembly' },
                { text: '11 — Compact System', link: '/chapters/11-compact-system' },
                { text: '12 — Startup Bootstrap', link: '/chapters/12-startup-bootstrap' },
                { text: '13 — Bridge System', link: '/chapters/13-bridge-system' },
                { text: '14 — UI State Management', link: '/chapters/14-ui-state-management' },
                { text: '15 — Services & API Layer', link: '/chapters/15-services-api-layer' },
                { text: '16 — Infrastructure & Config', link: '/chapters/16-infrastructure-config' },
                { text: '17 — Telemetry & Privacy', link: '/chapters/17-telemetry-privacy-operations' },
              ],
            },
          ],
        },
        editLink: {
          pattern: 'https://github.com/openedclaude/claude-reviews-claude/edit/main/docs/:path',
          text: 'Edit this page on GitHub',
        },
        footer: {
          message: 'Released under the MIT License.',
          copyright: 'Analysis by Claude · Code by Anthropic, PBC',
        },
        docFooter: {
          prev: '← Previous Chapter',
          next: 'Next Chapter →',
        },
      },
    },

    'zh-CN': {
      label: '简体中文',
      lang: 'zh-CN',
      link: '/zh-CN/',
      themeConfig: {
        nav: [
          { text: '架构总纲', link: '/zh-CN/overview' },
          { text: '章节导读', link: '/zh-CN/chapters/01-query-engine' },
          { text: 'GitHub', link: 'https://github.com/openedclaude/claude-reviews-claude' },
        ],
        sidebar: {
          '/zh-CN/': [
            {
              text: '开始阅读',
              items: [
                { text: '架构总纲', link: '/zh-CN/overview' },
              ],
            },
            {
              text: '架构深度分析',
              items: [
                { text: '01 — 查询引擎', link: '/zh-CN/chapters/01-query-engine' },
                { text: '02 — 工具系统', link: '/zh-CN/chapters/02-tool-system' },
                { text: '03 — 多智能体协调器', link: '/zh-CN/chapters/03-coordinator' },
                { text: '04 — 插件系统', link: '/zh-CN/chapters/04-plugin-system' },
                { text: '05 — 钩子系统', link: '/zh-CN/chapters/05-hook-system' },
                { text: '06 — Bash 执行引擎', link: '/zh-CN/chapters/06-bash-engine' },
                { text: '07 — 权限流水线', link: '/zh-CN/chapters/07-permission-pipeline' },
                { text: '08 — Swarm 智能体', link: '/zh-CN/chapters/08-agent-swarms' },
                { text: '09 — 会话持久化', link: '/zh-CN/chapters/09-session-persistence' },
                { text: '10 — 上下文装配', link: '/zh-CN/chapters/10-context-assembly' },
                { text: '11 — 压缩系统', link: '/zh-CN/chapters/11-compact-system' },
                { text: '12 — 启动与引导', link: '/zh-CN/chapters/12-startup-bootstrap' },
                { text: '13 — 桥接系统', link: '/zh-CN/chapters/13-bridge-system' },
                { text: '14 — UI 与状态管理', link: '/zh-CN/chapters/14-ui-state-management' },
                { text: '15 — 服务与 API 层', link: '/zh-CN/chapters/15-services-api-layer' },
                { text: '16 — 基础设施与配置', link: '/zh-CN/chapters/16-infrastructure-config' },
                { text: '17 — 遥测、隐私与运营', link: '/zh-CN/chapters/17-telemetry-privacy-operations' },
              ],
            },
          ],
        },
        editLink: {
          pattern: 'https://github.com/openedclaude/claude-reviews-claude/edit/main/docs/:path',
          text: '在 GitHub 上编辑此页',
        },
        footer: {
          message: '基于 MIT 许可证发布',
          copyright: '分析：Claude · 代码：Anthropic, PBC',
        },
        docFooter: {
          prev: '← 上一章',
          next: '下一章 →',
        },
        lastUpdated: {
          text: '最后更新于',
        },
        outline: {
          label: '本页目录',
        },
        returnToTopLabel: '返回顶部',
        sidebarMenuLabel: '菜单',
        darkModeSwitchLabel: '主题',
      },
    },
  },

  markdown: {
    lineNumbers: true,
    image: {
      lazyLoading: true,
    },
  },
})
