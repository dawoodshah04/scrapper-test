sudo docker run -d \
  -p 11235:11235 \
  --shm-size=2g \
  -e CRAWL4AI_API_TOKEN=2fe90f64dbaa1f2167d7f62663d33db5db47c2a51ef791d2ecc10fae77e3019b \
  --name crawl4ai \
  unclecode/crawl4ai:latest