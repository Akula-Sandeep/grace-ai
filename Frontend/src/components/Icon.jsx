function Icon({ name, size = 18, strokeWidth = 1.8 }) {
  const common = {
    width: size,
    height: size,
    viewBox: '0 0 24 24',
    fill: 'none',
    stroke: 'currentColor',
    strokeWidth,
    strokeLinecap: 'round',
    strokeLinejoin: 'round',
    'aria-hidden': 'true',
  };

  const paths = {
    thumbsUp: <><path d="M7 10v11" /><path d="M15 5.9 14 10h5.8a2 2 0 0 1 2 2.4l-1.4 7a2 2 0 0 1-2 1.6H7a2 2 0 0 1-2-2v-7a2 2 0 0 1 2-2h2.8a2 2 0 0 0 1.8-1.1L14.5 3a3 3 0 0 1 .5 2.9z" /></>,
    thumbsDown: <><path d="M17 14V3" /><path d="M9 18.1 10 14H4.2a2 2 0 0 1-2-2.4l1.4-7a2 2 0 0 1 2-1.6H17a2 2 0 0 1 2 2v7a2 2 0 0 1-2 2h-2.8a2 2 0 0 0-1.8 1.1L9.5 21a3 3 0 0 1-.5-2.9z" /></>,
    refresh: <><path d="M20 11a8 8 0 0 0-14.6-4.5L4 8" /><path d="M4 4v4h4" /><path d="M4 13a8 8 0 0 0 14.6 4.5L20 16" /><path d="M20 20v-4h-4" /></>,
    more: <><circle cx="5" cy="12" r="1.3" /><circle cx="12" cy="12" r="1.3" /><circle cx="19" cy="12" r="1.3" /></>,
    chevronDown: <path d="m6 9 6 6 6-6" />,
    chevronRight: <path d="m9 6 6 6-6 6" />,
    share: <><path d="M12 3v13" /><path d="m7 8 5-5 5 5" /><path d="M5 14v5a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2v-5" /></>,
    download: <><path d="M12 3v13" /><path d="m7 11 5 5 5-5" /><path d="M5 21h14" /></>,
    archive: <><rect x="3" y="4" width="18" height="5" rx="1" /><path d="M5 9v10a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9" /><path d="M10 13h4" /></>,
    link: <><path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1" /><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1" /></>,
    clapper: <><path d="M4 11h16v8a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2v-8z" /><path d="m4 11-.6-3.4a2 2 0 0 1 1.6-2.3l11.8-2.1a2 2 0 0 1 2.3 1.6L19.6 7 4 11z" /><path d="m8.5 4.6 2.2 4" /><path d="m13.5 3.7 2.2 4" /></>,
    eye: <><path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z" /><circle cx="12" cy="12" r="3" /></>,
    eyeOff: <><path d="M3 3l18 18" /><path d="M10.6 5.6A9.7 9.7 0 0 1 12 5.5c6 0 9.5 6.5 9.5 6.5a17 17 0 0 1-3.2 3.9" /><path d="M6.6 6.6A17 17 0 0 0 2.5 12S6 18.5 12 18.5a9.4 9.4 0 0 0 4.3-1" /><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2" /></>,
    mail: <><rect x="3" y="5" width="18" height="14" rx="2" /><path d="m3.5 6.5 8.5 6.5 8.5-6.5" /></>,
    alert: <><circle cx="12" cy="12" r="9" /><path d="M12 7.5v5.5" /><path d="M12 16.5h.01" /></>,
    spark: <><path d="M12 2l2.2 6.2L20 11l-5.8 2.2L12 20l-2.2-6.8L4 11l5.8-2.8L12 2z" /><path d="M19 17l.8 2.2L22 20l-2.2.8L19 23l-.8-2.2L16 20l2.2-.8L19 17z" /></>,
    search: <><circle cx="11" cy="11" r="6.5" /><path d="m16 16 4.2 4.2" /></>,
    message: <><path d="M20 11.5a7.5 7.5 0 0 1-7.5 7.5H7l-4 3v-6.2A7.4 7.4 0 0 1 4.5 5.3 7.5 7.5 0 0 1 20 11.5z" /></>,
    bookmark: <><path d="M6 4.5A2.5 2.5 0 0 1 8.5 2h7A2.5 2.5 0 0 1 18 4.5V22l-6-3.7L6 22V4.5z" /></>,
    check: <path d="m5 12 4 4L19 6" />,
    user: <><circle cx="12" cy="8" r="3.5" /><path d="M5 21a7 7 0 0 1 14 0" /></>,
    heart: <path d="M20.8 8.8c0 5-8.8 10.2-8.8 10.2S3.2 13.8 3.2 8.8A5 5 0 0 1 12 5.4a5 5 0 0 1 8.8 3.4z" />,
    compass: <><circle cx="12" cy="12" r="9" /><path d="m15.5 8.5-2.1 4.9-4.9 2.1 2.1-4.9 4.9-2.1z" /></>,
    logout: <><path d="M10 5H5v14h5" /><path d="M14 8l4 4-4 4" /><path d="M9 12h9" /></>,
    menu: <><path d="M4 7h16" /><path d="M4 12h16" /><path d="M4 17h16" /></>,
    close: <><path d="m6 6 12 12" /><path d="m18 6-12 12" /></>,
    arrow: <><path d="M12 19V5" /><path d="m6 11 6-6 6 6" /></>,
    copy: <><rect x="8" y="8" width="11" height="11" rx="2" /><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" /></>,
    edit: <><path d="m4 16.5-.7 3.2 3.2-.7L18.2 7.3a2.1 2.1 0 0 0-3-3L4 16.5z" /><path d="m13.8 5.2 3 3" /></>,
    trash: <><path d="M3 6h18" /><path d="M8 6V4h8v2" /><path d="m19 6-1 14H6L5 6" /><path d="M10 11v5" /><path d="M14 11v5" /></>,
    back: <><path d="M19 12H5" /><path d="m12 19-7-7 7-7" /></>,
    play: <path d="m9 6 10 6-10 6V6z" />,
    bolt: <><path d="m13 2-3 8h7l-6 12 1-9H5l8-11z" /></>,
    music: <><path d="M9 18V5l12-2v13" /><circle cx="6" cy="18" r="3" /><circle cx="18" cy="16" r="3" /></>,
    film: <><rect x="3" y="3" width="18" height="18" rx="2" /><path d="M7 3v18M17 3v18M3 8h4m-4 8h4m10-8h4m-4 8h4" /></>,
    award: <><circle cx="12" cy="8" r="6" /><path d="m8.2 13-1.3 9 5.1-3 5.1 3-1.3-9" /></>,
    pin: <><path d="M20 10c0 5-8 12-8 12S4 15 4 10a8 8 0 1 1 16 0z" /><circle cx="12" cy="10" r="2.5" /></>,
    lightbulb: <><path d="M9 18h6m-5 4h4m-2-21a7 7 0 0 0-4 12.7c.8.6 1 1.3 1 2.3h6c0-1 .2-1.7 1-2.3A7 7 0 0 0 12 1z" /></>,
    star: <path d="m12 3 2.7 5.5 6.1.9-4.4 4.3 1 6.1-5.4-2.9-5.4 2.9 1-6.1-4.4-4.3 6.1-.9L12 3z" />,
    plus: <><path d="M12 5v14" /><path d="M5 12h14" /></>,
    image: <><rect x="3" y="4" width="18" height="16" rx="2" /><circle cx="9" cy="10" r="2" /><path d="m21 16-5-5-9 9" /></>,
    bell: <><path d="M6 8a6 6 0 1 1 12 0c0 7 3 9 3 9H3s3-2 3-9" /><path d="M10.3 21a1.9 1.9 0 0 0 3.4 0" /></>,
    settings: <><circle cx="12" cy="12" r="3" /><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z" /></>,
    mic: <><rect x="9" y="2" width="6" height="12" rx="3" /><path d="M5 10a7 7 0 0 0 14 0" /><path d="M12 17v5" /></>,
    paperclip: <path d="m21 11-8.6 8.6a5 5 0 0 1-7-7L14 4a3.5 3.5 0 0 1 5 5l-8.6 8.6a2 2 0 0 1-2.8-2.8L15.5 7" />,
    speaker: <><path d="M11 5 6 9H3v6h3l5 4V5z" /><path d="M15.5 8.5a5 5 0 0 1 0 7" /><path d="M18.5 5.5a9 9 0 0 1 0 13" /></>,
    panelClose: (
  <>
    <rect x="3" y="4" width="18" height="16" rx="2" />
    <path d="M9 4v16" />
    <path d="m15 9-3 3 3 3" />
  </>
),
panelOpen: (
  <>
    <rect x="3" y="4" width="18" height="16" rx="2" />
    <path d="M9 4v16" />
    <path d="m12 9 3 3-3 3" />
  </>
),
arrowLeft: (
  <path d="M19 12H5m7-7-7 7 7 7" />
),
arrowRight: (
  <path d="M5 12h14m-7-7 7 7-7 7" />
),
folder: (
  <path d="M3.5 7.5A2 2 0 0 1 5.5 5.5h3.6l2 2.2h7.4a2 2 0 0 1 2 2v7.8a2 2 0 0 1-2 2H5.5a2 2 0 0 1-2-2z" />
),
  };

  return <svg {...common}>{paths[name] || paths.spark}</svg>;
}

export default Icon;
