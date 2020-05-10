var callback = function(details) {
        var send_headers = {}
        for (var i = 0; i < details.requestHeaders.length; ++i) {
            send_headers[details.requestHeaders[i].name] = details.requestHeaders[i].value
        }
        chrome.storage.sync.get({
          url: "http://127.0.0.1:8080"
        }, function(items) {
            var url = items.url;
            console.log(url);
            fetch(url, { headers: send_headers } )
                .then((response) => {
                    console.log(response.ok)
                    chrome.browserAction.setIcon({path: 'images/green.png'});
                })
                .catch((error) => {
                      console.log(error);
                      chrome.browserAction.setIcon({path: 'images/red.png'});
                });
        });
};

var filter = { urls: [
    "https://pkg.yourhero.ru/*",
    "https://developer.chrome.com/*",
    "https://utas.external.s2.fut.ea.com/*",
    "https://utas.external.s3.fut.ea.com/*"
]};

chrome.webRequest.onSendHeaders.addListener( callback, filter, [ 'requestHeaders' ]);
