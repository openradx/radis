# RADIS

<div class="slideshow-container">

  <div class="slides-wrapper">
    <div class="slide">
      <img src="assets/screenshots/Screenshot01_radis.png" alt="RADIS Screenshot 1">
    </div>
    <div class="slide">
      <img src="assets/screenshots/Screenshot02_radis.png" alt="RADIS Screenshot 2">
    </div>
    <div class="slide">
      <img src="assets/screenshots/Screenshot03_radis.png" alt="RADIS Screenshot 3">
    </div>
    <div class="slide">
      <img src="assets/screenshots/Screenshot04_radis.png" alt="RADIS Screenshot 4">
    </div>
  </div>

<a class="prev" onclick="changeSlide(-1)">❮</a>
<a class="next" onclick="changeSlide(1)">❯</a>

  <div class="dot-container">
    <span class="dot" onclick="currentSlide(0)"></span>
    <span class="dot" onclick="currentSlide(1)"></span>
    <span class="dot" onclick="currentSlide(2)"></span>
    <span class="dot" onclick="currentSlide(3)"></span>
  </div>

</div>

<style>
  .slideshow-container {
    position: relative;
    max-width: 100%;
    margin: 2rem auto;
    overflow: hidden;
    border-radius: 8px;
    box-shadow: 0 6px 20px rgba(0,0,0,0.15);
  }

  .slides-wrapper {
    display: flex;
    transition: transform 0.6s ease-in-out;
    width: 100%;
  }

  .slide {
    min-width: 100%;
  }

  .slide img {
    width: 100%;
    display: block;
  }

  /* Navigation buttons */
  .prev, .next {
    cursor: pointer;
    position: absolute;
    top: 50%;
    padding: 12px;
    color: white;
    font-size: 18px;
    background-color: rgba(0,0,0,0.5);
    user-select: none;
    transform: translateY(-50%);
    border-radius: 3px;
  }

  .next {
    right: 10px;
  }

  .prev {
    left: 10px;
  }

  .prev:hover, .next:hover {
    background-color: rgba(0,0,0,0.8);
  }

  /* Dots */
  .dot-container {
    text-align: center;
    position: absolute;
    bottom: 10px;
    width: 100%;
  }

  .dot {
    cursor: pointer;
    height: 12px;
    width: 12px;
    margin: 0 4px;
    background-color: rgba(255,255,255,0.5);
    border-radius: 50%;
    display: inline-block;
  }

  .dot.active {
    background-color: rgba(255,255,255,0.9);
  }
</style>

<script>
  let slideIndex = 0;
  let timer;

  function showSlide(index) {
    const slidesWrapper = document.querySelector(".slides-wrapper");
    const dots = document.querySelectorAll(".dot");
    const totalSlides = dots.length;

    if (index >= totalSlides) slideIndex = 0;
    if (index < 0) slideIndex = totalSlides - 1;

    slidesWrapper.style.transform = `translateX(-${slideIndex * 100}%)`;

    dots.forEach(dot => dot.classList.remove("active"));
    dots[slideIndex].classList.add("active");

    clearTimeout(timer);
    timer = setTimeout(() => {
      slideIndex++;
      showSlide(slideIndex);
    }, 4000);
  }

  function changeSlide(n) {
    slideIndex += n;
    showSlide(slideIndex);
  }

  function currentSlide(n) {
    slideIndex = n;
    showSlide(slideIndex);
  }

  document.addEventListener("DOMContentLoaded", () => {
    showSlide(slideIndex);
  });
</script>

## Radiology Report Archive and Discovery System

RADIS is an open-source web application designed to enhance the management, retrieval, and analysis of radiology reports within hospital infrastructures.

## Why RADIS?

Healthcare providers need efficient ways to search, organize, and analyze radiology reports. RADIS addresses these challenges with modern search technologies and AI-powered tools.

## What RADIS Does

RADIS serves as a comprehensive platform for managing radiology reports, enabling efficient search, organization, and collaboration.

### How It Works

**RADIS acts as an intelligent hub** for radiology reports:

1. **Report Storage**: Securely store radiology reports with structured metadata
2. **Advanced Search**: Use hybrid search combining traditional text search with semantic understanding
3. **AI-Powered Analysis**: Leverage large language models for intelligent filtering and categorization
4. **Collections**: Organize reports into custom collections for easy access and review
5. **Subscription**: Subscribe to searches and get notified when new matching reports arrive
6. **Notes**: Add personal notes to reports for annotations and context

**Ready to modernize your radiology workflow?** RADIS combines traditional database functionality with cutting-edge AI to make radiology reports more accessible and actionable.

## About

**Developed at**

[CCI Bonn](https://ccibonn.ai/) - Center for Computational Imaging, University Hospital Bonn

**in Partnership with**

- [Universitätsklinikum Bonn](https://www.ukbonn.de/)
- [Thoraxklinik Heidelberg](https://www.thoraxklinik-heidelberg.de/)
- [Universitätsklinikum Heidelberg](https://www.klinikum.uni-heidelberg.de/kliniken-institute/kliniken/diagnostische-und-interventionelle-radiologie/klinik-fuer-diagnostische-und-interventionelle-radiologie/)

!!! important "Beta Status"
RADIS is currently in an early phase of development. While we are actively building and refining its features, users should anticipate ongoing updates and potential breaking changes as the platform evolves. We appreciate your understanding and welcome feedback to help us shape the future of RADIS.

## Quick Start

1. **Admin Guide**: Learn to configure administrative features in our [Admin Guide](user-docs/admin-guide.md)
2. **User Guide**: Explore features in our [User Guide](user-docs/user-guide.md)
3. **Development**: Explore the architecture overview in our [development guide](dev-docs/architecture.md)

## License

RADIS is licensed under the [AGPL-3.0-or-later](https://github.com/openradx/radis/blob/main/LICENSE) license.
