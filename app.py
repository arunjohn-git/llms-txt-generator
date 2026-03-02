<!DOCTYPE html>
<html>
<head>
    <title>llms.txt Generator</title>
    <style>
        .progress-container { width: 100%; background: #eee; border-radius: 5px; margin: 20px 0; display:none; }
        #progress-bar { width: 0%; height: 25px; background: #764abc; border-radius: 5px; transition: width 0.3s; }
        #log { font-family: monospace; background: #f4f4f4; padding: 10px; height: 150px; overflow-y: scroll; }
    </style>
</head>
<body>
    <h1>llms.txt Generator</h1>
    <input type="text" id="url" placeholder="Enter Website URL" style="width: 300px;">
    <button onclick="startGeneration()">Generate</button>

    <div class="progress-container" id="pContainer">
        <div id="progress-bar"></div>
    </div>
    <p id="stats">Time Elapsed: 0s | Batch: 0/0</p>
    <div id="log">Waiting to start...</div>

    <script>
        async function startGeneration() {
            const url = document.getElementById('url').value;
            const log = document.getElementById('log');
            const bar = document.getElementById('progress-bar');
            const stats = document.getElementById('stats');
            document.getElementById('pContainer').style.display = 'block';

            const response = await fetch('/generate-stream', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({url: url})
            });

            const reader = response.body.getReader();
            const decoder = new TextDecoder();

            while (true) {
                const {value, done} = await reader.read();
                if (done) break;
                
                const chunk = decoder.decode(value);
                const lines = chunk.split('\n');
                
                lines.forEach(line => {
                    if (line.startsWith('data: ')) {
                        const data = JSON.parse(line.replace('data: ', ''));
                        
                        if (data.type === 'progress') {
                            bar.style.width = data.percent + '%';
                            stats.innerText = `Time Elapsed: ${data.elapsed}s | Batch: ${data.batch}/${data.total}`;
                            log.innerHTML += `<div>Processed batch ${data.batch}...</div>`;
                        } else if (data.type === 'final') {
                            log.innerHTML += `<div style="color:green">Done! Total time: ${data.elapsed}s</div>`;
                            console.log(data.content);
                        }
                    }
                });
            }
        }
    </script>
</body>
</html>
