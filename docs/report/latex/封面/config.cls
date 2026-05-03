\ProvidesClass{config}[2023/10/01 v1.3 Professional Experiment Report Class]

%---------------------------------------------------------------------------------
% 0. 预处理选项
%---------------------------------------------------------------------------------
% 将所有未显式处理的选项（如 10.5pt）传递给底层的 article 类
\DeclareOption*{\PassOptionsToClass{\CurrentOption}{article}}
\ProcessOptions\relax

% 加载基础文档类 (A4纸)
\LoadClass[a4paper]{article}

%---------------------------------------------------------------------------------
% 1. 字体与引擎配置 (XeLaTeX)
%---------------------------------------------------------------------------------
\RequirePackage{iftex}
\RequirePackage{xeCJK}
\RequirePackage{fontspec}
\setlength{\parindent}{2em}
% 中文字体配置
\setCJKmainfont[
    Path = ./,
    BoldFont = simsunBold.otf,
    ItalicFont = simsun.ttc,
    AutoFakeSlant = true
]{simsun.ttc}

% 无衬线和等宽字体均回退到宋体
\setCJKsansfont[Path = ./]{simsun.ttc}
\setCJKmonofont[Path = ./]{simsun.ttc}

% 英文字体配置
\setmainfont{Times New Roman}

%---------------------------------------------------------------------------------
% 2. 核心宏包加载
%---------------------------------------------------------------------------------
% 2.1 颜色与绘图
\RequirePackage[dvipsnames]{xcolor}
\RequirePackage{tikz}
\RequirePackage{pgfplots}
\pgfplotsset{compat=1.18}
\usetikzlibrary{patterns, positioning, arrows.meta, calc, decorations.pathmorphing, backgrounds, shapes.geometric}

% 2.2 数学支持
\RequirePackage{amsmath}
\RequirePackage{mathtools}
\RequirePackage{zhnumber}

% 2.3 图表支持
\RequirePackage{graphicx}
\RequirePackage{float}
\RequirePackage{caption}
\RequirePackage{subcaption}
\RequirePackage{booktabs}
\RequirePackage{array}

% === 智能图片插入 ===
\newcommand{\safeincludegraphics}[2][]{%
    \IfFileExists{#2}{%
        \includegraphics[#1]{#2}%
    }{%
        \IfFileExists{figures/#2}{%
            \includegraphics[#1]{figures/#2}%
        }{%
             \IfFileExists{images/#2}{%
                \includegraphics[#1]{images/#2}%
            }{%
                \begin{tikzpicture}
                    \node[
                        draw,
                        fill=black!10,
                        minimum width=\linewidth,
                        minimum height=4cm,
                        text width=\linewidth,
                        align=center,
                        font=\ttfamily\small
                    ] {
                        \textbf{[图片待插入]} \\[.5em]
                        文件名: #2 \\
                        (未找到文件，请检查路径或文件名)
                    };
                \end{tikzpicture}%
            }%
        }%
    }%
}

% 2.4 页面布局
\RequirePackage{geometry}
\RequirePackage{titlesec}
\RequirePackage{fancyhdr}
\RequirePackage{indentfirst}
\RequirePackage{enumitem}

% 2.5 代码高亮
\RequirePackage{listings}
\lstset{
    language=Matlab,
    basicstyle=\ttfamily\small,
    breaklines=true,
    keywordstyle=\bfseries\color{NavyBlue},
    commentstyle=\itshape\color{gray},
    stringstyle=\bfseries\color{PineGreen!90!black},
    columns=flexible,
    numbers=left,
    numbersep=1em,
    numberstyle=\footnotesize,
    frame=shadowbox,
    rulesepcolor=\color{red!20!green!20!blue!20},
    showspaces=false,
    showstringspaces=false,
    showtabs=false,
    tabsize=4
}
% 2.6 参考文献支持
\RequirePackage[numbers, sort&compress]{natbib}
\setlength{\bibsep}{0.5ex}
\RequirePackage[normalem]{ulem}

% 2.7 超链接
\RequirePackage{hyperref}
\hypersetup{
    colorlinks=true,
    linkcolor=black,
    citecolor=black,
    urlcolor=blue,
    pdfborder={0 0 0}
}

%---------------------------------------------------------------------------------
% 3. 界面中文化与重定义 (修改了此处)
%---------------------------------------------------------------------------------
\renewcommand{\contentsname}{目\quad 录}
\renewcommand{\listfigurename}{插图目录}
\renewcommand{\listtablename}{表格目录}
\renewcommand{\refname}{参考文献}
\renewcommand{\abstractname}{摘\quad 要}
\renewcommand{\figurename}{图}
\renewcommand{\tablename}{表}

% === 修改：重定义摘要环境 ===
\renewenvironment{abstract}{
    % 1. 强制显示页眉 (覆盖 main.tex 可能的 empty 设置)
    \thispagestyle{fancy}
    % 2. 顶部增加 1cm 留白，防止文字太靠上导致失衡
    \vspace*{-1.0cm}
    \begin{center}
        \bfseries \Large \abstractname % 标题字号加大到 Large
    \end{center}
    \vspace{0.5em}
    \par
    % 3. 正文字号调整为 Normal (原为 small)
    \normalsize
    % 使用 list 环境手动控制左右缩进 (2em)，保持正式感
    \begin{list}{}{
        \setlength{\leftmargin}{2em}
        \setlength{\rightmargin}{2em}
        \setlength{\listparindent}{2em} % 首行缩进
        \setlength{\itemindent}{\listparindent}
        \setlength{\parsep}{0pt}
    }
    \item\relax
}{
    \end{list}
    % 4. 底部增加 2cm 间距，与正文拉开距离
    \vspace{2cm}
}

%---------------------------------------------------------------------------------
% 4. 版面详细设置
%---------------------------------------------------------------------------------
\geometry{left=2.5cm, right=2.5cm, top=2.5cm, bottom=2.5cm}
\linespread{1.5}

% 页眉页脚设置
\pagestyle{fancy}
\fancyhf{}
\fancyhead[C]{\@expname}
\fancyfoot[C]{\thepage}

%---------------------------------------------------------------------------------
% 5. 信息录入字段定义
%---------------------------------------------------------------------------------
\newcommand{\major}[1]{\def\@major{#1}}
\newcommand{\name}[1]{\def\@name{#1}}
\newcommand{\stuid}[1]{\def\@stuid{#1}}
\newcommand{\lab}[1]{\def\@lab{#1}}
\newcommand{\course}[1]{\def\@course{#1}}
\newcommand{\instructor}[1]{\def\@instructor{#1}}
\newcommand{\grades}[1]{\def\@grades{#1}}
\newcommand{\expname}[1]{\def\@expname{#1}}
\newcommand{\exptype}[1]{\def\@exptype{#1}}
\newcommand{\partner}[1]{\def\@partner{#1}}

%---------------------------------------------------------------------------------
% 6. 封面页生成命令
%---------------------------------------------------------------------------------
\newcommand{\maketitlepage}{
    \begin{titlepage}
        \vspace*{1cm}
        \begin{center}
            \Huge{\textbf{\@course}}\\[0.5cm]
            \Huge{\textbf{实验报告}}
        \end{center}
        \vspace{3cm}
        \begin{center}
            \Large
            \renewcommand{\arraystretch}{1.5}
            \begin{tabular}{rl}
                \textbf{实验名称：} & \underline{\makebox[220pt]{\@expname}} \\
                \textbf{实验地点：} & \underline{\makebox[220pt]{\@lab}} \\
                \textbf{姓\quad 名：} & \underline{\makebox[220pt]{\@name}} \\
                \textbf{学\quad 号：} & \underline{\makebox[220pt]{\@stuid}} \\
                \textbf{实验日期：} & \underline{\makebox[220pt]{\today}} \\
                \textbf{指导老师：} & \underline{\makebox[220pt]{\@instructor}} \\
            \end{tabular}
        \end{center}
        \vfill
        \begin{center}
            \large \today
        \end{center}
    \end{titlepage}
}

%---------------------------------------------------------------------------------
% 7. 目录页生成命令
%---------------------------------------------------------------------------------
\newcommand{\maketoc}{
    \newpage
    {
        \hypersetup{linkcolor=black}
        % --- 修改开始 ---
        \linespread{1.15}\selectfont  % 设置行距倍数，0.9 表示 0.9倍行距，数值越小越密
        \setlength{\parskip}{0pt}    % 可选：将段落间距设为0，进一步拉紧
        % --- 修改结束 ---
        \tableofcontents
    }
    \thispagestyle{empty}
    \newpage
    \setcounter{page}{1}
}