// The fixture's own helper: where the made-up product is. It is opened as local files, so a test needs no server.
const path = require('path');
const { pathToFileURL } = require('url');

const ROOT = path.resolve(__dirname, '..', '..', 'product');

/** Address of a page of the product: url('index.html'), url('index.html', 'seed=minor'). */
const url = (page, query) => pathToFileURL(path.join(ROOT, page)).href + (query ? `?${query}` : '');

module.exports = { url };
