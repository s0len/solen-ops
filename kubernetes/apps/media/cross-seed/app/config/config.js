// If you find yourself always using the same command-line flag, you can set it
// here as a default.

module.exports = {

  apiKey: `{{ .CROSS_SEED_API_KEY }}`,

  torznab: [
    1, 2, 6, 7, 10, 8, 11, 16, 9, 5, 18, 3
  ].map(i => `http://prowlarr.media.svc.cluster.local/$${i}/api?apikey=$${process.env.PROWLARR_API_KEY}`),

  sonarr: [
    `http://sonarr-standard.media.svc.cluster.local/?apikey=$${process.env.SONARR_API_KEY}`,
  ],

  radarr: [
    `http://radarr-standard.media.svc.cluster.local/?apikey=$${process.env.RADARR_API_KEY}`,
  ],

  host: undefined,

  port: 2468,

  notificationWebhookUrl: undefined,

  rtorrentRpcUrl: undefined,

  qbittorrentUrl: `http://$${process.env.QBIT_USER}:$${process.env.QBIT_PASS}@qbittorrent.media.svc.cluster.local`,

  transmissionRpcUrl: undefined,

  delugeRpcUrl: undefined,

  delay: 90,

  dataDirs: ["/data/torrents/series", "/data/torrents/movies", "/data/torrents/music"],

  linkCategory: "cross-seed",

  linkDir: undefined,

  linkType: "hardlink",

  flatLinking: false,

  matchMode: "safe",

  maxDataDepth: 2,

  torrentDir: "/qbittorrent/qBittorrent/BT_backup",

  outputDir: "/data/torrents/cross-seeds",

  includeSingleEpisodes: false,

  includeNonVideos: true,

  fuzzySizeThreshold: 0.02,

  excludeOlder: "6 days",

  excludeRecentSearch: "3 days",

  action: "inject",

  duplicateCategories: false,

  rssCadence: "30 minutes",

  searchCadence: "1 day",

  snatchTimeout: "1min",

  searchTimeout: "2 minutes",

  searchLimit: null,

  blockList: undefined,
};
