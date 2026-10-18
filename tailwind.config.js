// Tailwind build for web_dashboard/static/css/tailwind.css. See scripts/build_css.sh.
//
// `content` is every file a class name can come from. Python is in the list because
// services and API responses carry class strings (status badges, tile colours) that the
// browser renders: a class only in a .py file is still a class a page shows.
module.exports = {
  content: [
    './web_dashboard/templates/**/*.html',
    './web_dashboard/static/js/**/*.js',
    '!./web_dashboard/static/js/vendor/**',
    './web_dashboard/**/*.py',
  ],
  theme: { extend: {} },
  plugins: [],
};
