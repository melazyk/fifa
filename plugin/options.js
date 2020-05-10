// Saves options to chrome.storage
function save_options() {
  var url = document.getElementById('url').value;
  chrome.storage.sync.set({
    url: url
  }, function() {
    // Update status to let user know options were saved.
    var status = document.getElementById('status');
    status.textContent = 'Options saved.';
    setTimeout(function() {
      status.textContent = '';
    }, 750);
  });
}

// Restores select box and checkbox state using the preferences
// stored in chrome.storage.
function restore_options() {
  // Use default value color = 'red' and likesColor = true.
  chrome.storage.sync.get({
    url: "http://127.0.0.1:8080"
  }, function(items) {
    document.getElementById('url').value = items.url;
  });
}

document.addEventListener('DOMContentLoaded', restore_options);
document.getElementById('save').addEventListener('click', save_options);
