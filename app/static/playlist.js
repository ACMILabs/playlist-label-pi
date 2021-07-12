/**
 * Class grouping together all methods to render a playlist label in the client,
 * along with simple state to track the current playlist being played.
 */

const FPS = 5;

export default class PlaylistLabelRenderer {
  /**
   * Set an initial state for the renderer
   */

  constructor() {
    this.state = {
      currentLabelId: null,
      nextLabelId: null,
      items: null,
      upcomingItems: null,
      isAnimatingCollect: false,
      playbackPosition: 0,
      collectClassname: null,
      errorDialogueCloseTimeout: null,
    };
  }

  /**
   * Init, parsing playlist id that should be made available on the
   * rendered page via window.initialData.
   */
  init() {
    const id = "id" in window.initialData ? window.initialData.id : null;
    this.state.currentLabelId =
      "current_label_id" in window.initialData
        ? window.initialData.current_label_id
        : null;
    this.state.nextLabelId =
      "next_label_id" in window.initialData
        ? window.initialData.next_label_id
        : null;
    this.state.collectClassname =
      "collect_classname" in window.initialData
        ? window.initialData.collect_classname
        : "collect";

    if (id != null) {
      this.fetchPlaylist(`/api/playlist/`);
    } else {
      console.error("No valid id could be found on initial pageload."); // eslint-disable-line no-console
    }

    if (
      typeof window.initialData.ignore_tap_reader === "undefined" ||
      !window.initialData.ignore_tap_reader
    ) {
      this.handleTapMessage = this.handleTapMessage.bind(this);
      const tapSource = new EventSource("/api/tap-source/");
      tapSource.onmessage = this.handleTapMessage;
    }

    if (
      typeof window.initialData.ignore_media_player !== "undefined" &&
      window.initialData.ignore_media_player
    ) {
      setInterval(this.autoUpdateProgress, 1000 / FPS, this);
    }
  }

  hashChange() {
    const labelId = parseInt(window.location.hash.substring(1), 10);
    this.jumpToLabel(labelId);
    this.state.playbackPosition = 0;
    this.updateProgress();
  }

  onPlaylistData(jsonData) {
    this.state.items = jsonData.playlist_labels;
    this.state.upcomingItems = jsonData.playlist_labels;
    document.onkeydown = this.onKeyPress.bind(this);
    window.onhashchange = this.hashChange.bind(this);
    if (
      typeof window.initialData.ignore_media_player === "undefined" ||
      !window.initialData.ignore_media_player
    ) {
      this.subscribeToMediaPlayer(jsonData);
    }
    this.addTitleAnnotation(jsonData.playlist_labels[0].label.work);
    if (window.location.hash) {
      this.hashChange();
    } else {
      window.location.hash = this.state.items[0].label.id;
    }
  }

  /**
   * Fetch playlist makes API request to get playlist data and calls {@link subscribeToMediaPlayer}.
   * @param {string} url - The nfcTag API endpoint with primary key already included.
   */
  fetchPlaylist(url) {
    if (url && !window.initialData.is_preview) {
      fetch(url)
        .then((response) => {
          if (!response.ok) {
            throw Error(response.statusText);
          }
          return response.json();
        })
        .then(this.onPlaylistData.bind(this))
        .catch((error) => console.error(error)); // eslint-disable-line no-console
    } else {
      this.onPlaylistData(window.initialData.playlist_json);
    }
  }

  /**
   * Subscribe to the media player messages
   */
  subscribeToMediaPlayer() {
    // Subscribe to the media player messages
    // TODO: get media_player id from XOS
    const client = new Paho.MQTT.Client( // eslint-disable-line no-undef
      window.initialData.mqtt_host,
      parseInt(window.initialData.mqtt_port, 10),
      "/ws",
      ""
    );

    // set callback handlers
    client.onConnectionLost = this.onConnectionLost.bind(this);
    client.onMessageArrived = this.onMessageArrived.bind(this);
    client.connect({
      userName: window.initialData.mqtt_username,
      password: window.initialData.mqtt_password,
      onSuccess: () => {
        // Subscribe to the media player AMQP feed
        // TODO: Get the media player ID from XOS
        client.subscribe(
          `mediaplayer.${window.initialData.xos_media_player_id}`
        );
      },
      onFailure: () => {
        // Try to re-connect again
        this.subscribeToMediaPlayer();
      },
    });
  }

  onKeyPress(e) {
    if (e.keyCode >= 48 && e.keyCode <= 57) {
      // number keys
      this.state.playbackPosition = 0.1 * (e.keyCode - 48);
      this.updateProgress();
    }
    if (e.keyCode === 39) {
      // right arrow
      window.location.hash = this.state.nextLabelId;
    }
    if (e.keyCode === 84) {
      // 't'
      this.handleTapMessage();
    }
  }

  onConnectionLost(responseObject) {
    if (responseObject.errorCode !== 0) {
      console.error(`MQTT connection lost: ${responseObject.errorMessage}`); // eslint-disable-line no-console
      // Re-subscribe
      this.subscribeToMediaPlayer();
    }
  }

  onMessageArrived(message) {
    // Update display as needed based on message content
    const messageJson = JSON.parse(message.payloadString);

    // Update the progress bar
    this.state.playbackPosition = messageJson.playback_position;
    this.updateProgress();

    // Update the label if needed
    if (messageJson.label_id !== this.state.currentLabelId) {
      window.location.hash = messageJson.label_id;
    }
  }

  jumpToLabel(labelId) {
    // console.log('jump to label', labelId);
    // Update the current state
    this.state.currentLabelId = labelId;
    const { items } = this.state;

    // make a list of items that starts with the new label
    const upcomingItems = [];
    const startIndex = items.findIndex((item) => {
      return item.label && item.label.id === labelId;
    });
    for (let i = 0; i < items.length; i++) {
      upcomingItems.push(items[(startIndex + i) % items.length]);
    }

    this.state.upcomingItems = upcomingItems;

    // update label content
    this.updateMainLabelContent(upcomingItems[0]);
    if (items.length > 1) {
      this.state.nextLabelId = upcomingItems[1].label.id;
      this.updateUpNextContent(upcomingItems);
    }
  }

  updateMainLabelContent(item) {
    const { label } = item;
    // Update the label fields with the currently playing data
    try {
      document.getElementById("title").innerHTML = label.title;
      this.addTitleAnnotation(label.work);
      document.getElementById("subtitles").innerHTML = label.subtitles;
      document.getElementById("content0").innerHTML = label.columns[0].content;
      document.getElementById("content1").innerHTML = label.columns[1].content;
      document.getElementById("content2").innerHTML = label.columns[2].content;

      if (label.work.is_context_indigenous) {
        document.getElementById("indigenous").className =
          "indigenous indigenous_active";
      } else {
        document.getElementById("indigenous").className = "indigenous";
      }
    } catch (error) {
      // Pass for a countdown template without a title element
    }
  }

  updateUpNextContent(items) {
    // Update up next label
    const item = items[1];
    document.querySelector("#next_title").innerHTML = item.label.title;

    for (let i = 1; i < items.length; i++) {
      const { label } = items[i];
      const id = `#up_next_label_${i}`;
      try {
        document.querySelector(
          `${id} .title`
        ).innerHTML = `${i}. ${label.title}`;
        document.querySelector(`${id} .subtitles`).innerHTML = label.subtitles;
      } catch (err) {
        // continue regardless of error
      }
    }
  }

  updateProgress() {
    const progressBar = document.getElementById("progress-bar");
    const { playbackPosition } = this.state;
    progressBar.style.width = `${playbackPosition * 100}%`;

    const items = this.state.upcomingItems;

    // calculate time to wait times for the upcoming videos;
    let timeToWait = items[0].video.duration_secs * (1.0 - playbackPosition);
    const numMinutes = parseInt(Math.round(timeToWait / 60.0), 10);
    let unit = " minute";
    if (numMinutes !== 1) unit += "s";

    try {
      // Set countdown timer minutes remaining
      let countdownTime = numMinutes;
      let countdownUnit = " minute";
      if (countdownTime !== 1) unit += "s";
      let updateCountdownTime = true;
      if (timeToWait < 60) {
        countdownTime = parseInt(Math.round(timeToWait), 10);
        countdownUnit = " second";
        if (countdownTime % 10 !== 0 || countdownTime === 0) {
          updateCountdownTime = false;
        }
      }
      if (countdownTime !== 1) countdownUnit += "s";
      if (updateCountdownTime) {
        document.querySelector("#minutes_remaining").innerHTML = countdownTime;
        document.querySelector("#units").innerHTML = countdownUnit;
      }
    } catch (error) {
      // continue regardless of error
    }

    for (let i = 1; i < items.length; i++) {
      const id = `#up_next_label_${i}`;
      try {
        document.querySelector(`${id} .time_to_wait`).innerHTML =
          numMinutes + unit;
      } catch (error) {
        // continue regardless of error
      }
      try {
        timeToWait += items[i].video.duration_secs;
      } catch (error) {
        // continue regardless of error
      }
    }
  }

  autoUpdateProgress(slf) {
    // move progress bar along one frame, while we wait for the next message to arrive from the broker
    if (slf.state.upcomingItems && slf.state.playbackPosition < 1.0) {
      const duration = slf.state.upcomingItems[0].video.duration_secs;
      const portionPerFrame = 1.0 / (duration * FPS);
      slf.state.playbackPosition += portionPerFrame; // eslint-disable-line no-param-reassign
      slf.updateProgress();
    }
  }

  truncate(str, max) {
    return str.length > max ? `${str.substr(0, max - 3)}...` : str;
  }

  openErrorDialogue(errorText) {
    const errorDialogueElement = document.getElementById("error-dialogue");
    errorDialogueElement.style.opacity = 1;
    const errorDialogueTextElement = document.getElementById(
      "error-dialogue-text"
    );
    errorDialogueTextElement.innerHTML = errorText;
    window.clearTimeout(this.state.errorDialogueCloseTimeout);
    this.state.errorDialogueCloseTimeout = window.setTimeout(
      this.closeErrorDialogue.bind(this),
      3000
    );
  }

  closeErrorDialogue() {
    const errorDialogueElement = document.getElementById("error-dialogue");
    window.clearTimeout(this.state.errorDialogueCloseTimeout);
    errorDialogueElement.style.opacity = 0;
  }

  handleTapMessage(event) {
    const eventData = JSON.parse(event.data);
    const tapSuccessful =
      eventData.tap_successful && eventData.tap_successful === 1;

    if (!tapSuccessful) {
      this.openErrorDialogue(
        "Work not collected <br><br> See a Visitor Experience staff member"
      );
      return;
    }

    if (this.state.isAnimatingCollect) return;

    // UPDATE 'COLLECTED' UI
    // Debounced with isAnimatingCollect
    this.state.isAnimatingCollect = true;

    // Animation plays: collect -> hidden -> collected -> hidden -> collect
    const collectElement = document.getElementById("collect");
    const { collectClassname } = this.state;
    collectElement.className = `${collectClassname} hidden`;
    window.setTimeout(function timeout1() {
      collectElement.innerHTML = "COLLECTED";
      collectElement.className = `${collectClassname} active`;
    }, 500);
    window.setTimeout(function timeout2() {
      collectElement.className = `${collectClassname} active hidden`;
    }, 3000);
    window.setTimeout(
      function timeout3() {
        collectElement.className = collectClassname;
        collectElement.innerHTML = "COLLECT";
        this.state.isAnimatingCollect = false;
      }.bind(this),
      3500
    );
  }

  addTitleAnnotation(work) {
    // If a title_annotation exists, add it to the end of the title
    if (work && work.title_annotation) {
      try {
        const title = document.getElementById("title");
        const titleAnnotation = document.createElement("span");
        titleAnnotation.className = "title_annotation";
        titleAnnotation.innerHTML = work.title_annotation;
        title.innerHTML = title.innerHTML.replace(
          /<\/p>/g,
          `${titleAnnotation.outerHTML}</p>`
        );
      } catch (error) {
        // Pass for a countdown template without a title element
      }
    }
  }
}
