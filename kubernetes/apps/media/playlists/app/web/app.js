// Progressive enhancement only. With JS blocked every screen still works: the
// upload form keeps its own submit button and the loading page advances itself
// with a meta refresh.
(function () {
  "use strict";

  // Show the password, for anyone typing a long one on a phone keyboard.
  var toggle = document.getElementById("showpw");
  var pw = document.getElementById("pw");
  if (toggle && pw) {
    toggle.addEventListener("change", function () {
      pw.type = toggle.checked ? "text" : "password";
    });
  }

  // Picking a file and then hunting for a button is the dead end that loses
  // people, so name the file and submit immediately.
  var file = document.getElementById("fil");
  var form = document.getElementById("upform");
  var chosen = document.getElementById("chosen");
  var btn = document.getElementById("upbtn");
  var namn = document.getElementById("namn");

  if (file && form) {
    file.addEventListener("change", function () {
      if (!file.files || !file.files.length) return;
      var name = file.files[0].name;
      if (chosen) {
        chosen.textContent = name;
        chosen.hidden = false;
      }
      // Pre-fill the playlist name from the filename if he left it blank. The
      // server validates and caps this regardless of what arrives.
      if (namn && !namn.value) {
        namn.value = name.replace(/\.[^.]+$/, "");
      }
      if (btn) {
        btn.disabled = true;
        btn.textContent = "Läser filen …";
      }
      form.submit();
    });
  }
})();
